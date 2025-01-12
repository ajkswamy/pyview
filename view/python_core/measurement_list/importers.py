import pathlib as pl
from tillvisionio.vws import VWSDataManager
import pandas as pd
import tifffile
from view.python_core.misc import excel_datetime
import typing
import easygui
import logging
import pprint
from abc import ABC, abstractmethod


def calculate_dt_from_timing_ms(timing_ms: str) -> float:

    times = timing_ms.strip()
    times = [float(x) for x in times.split(' ')]
    # calculate frame rate as time of (last frame - first frame) / (frames-1)
    dt = (times[-1] - times[0]) / (len(times) - 1)
    return dt


# a function injecting code into the automatic parsing of metadata from vws.log file
def additional_cols_func(s):

    # time & analyze
    try:
        dt = calculate_dt_from_timing_ms(s["Timing_ms"])
        analyze = 1  # since there are at least two frames, and thus a time, I suppose it is worth analyzing
    except Exception as e:
        dt = -1
        analyze = 0

    return {"dt": dt, "Analyze": analyze}


class BaseImporter(ABC):

    def __init__(self, default_values: typing.Mapping):

        super().__init__()
        self.default_values = default_values
        self.associated_extensions = None
        self.associate_file_type = None
        self.LE_loadExp = None
        self.movie_data_extensions = None

    def get_default_row(self):

        return pd.Series(self.default_values)

    def import_metadata(self, raw_data_files, measurement_filter):

        combined_df = pd.DataFrame()

        for fle_ind, fle in enumerate(raw_data_files):

            logging.getLogger("VIEW").info(f"Parsing metadata from {fle}")
            df = self.read_single_measurement_metadata(fle, fle_ind, measurement_filter)

            combined_df = combined_df.append(df, ignore_index=True)

        return combined_df

    @abstractmethod
    def read_single_measurement_metadata(self, fle: str, fle_ind: int,
                                         measurement_filter: typing.Callable[[pd.Series], bool]) -> pd.DataFrame:

        pass

    def get_filetype_info_string(self):

        return [f"*{x}" for x in self.associated_extensions] + [self.associate_file_type]

    def ask_for_files(self, default_dir, multiple: bool=True) -> dict:

        default_dir_str = str(pl.Path(default_dir) / "*")
        files_chosen = easygui.fileopenbox(
            title=f"Choose one or more files for LE_loadExp={self.LE_loadExp}",
            filetypes=self.get_filetype_info_string(),
            multiple=multiple,
            default=default_dir_str)
        if files_chosen is None:
            raise IOError("User Abort while choosing files.")
        else:
            assert files_chosen[0].startswith(str(default_dir)), \
                f"The data selected in not in the expected data directory of the current tree:\n" \
                f"{default_dir}. Please copy your data there and try again!"
            animal_tag_raw_data_mapping = self.get_animal_tag_raw_data_mapping(files_chosen)
            logging.getLogger("VIEW").info(
                f"Working on the following animal tags and their corresponding files:\n"
                f"{pprint.pformat(animal_tag_raw_data_mapping)}")
            return animal_tag_raw_data_mapping

    @abstractmethod
    def get_animal_tag_raw_data_mapping(self, files_chosen: list) -> dict:

        pass

    @abstractmethod
    def get_path_relative_to_data_dir(self, fle):

        pass


class TillImporter(BaseImporter):

    def __init__(self, default_values: typing.Mapping):

        super().__init__(default_values)
        self.associate_file_type = "VWS Log Files"
        self.associated_extensions = [".vws.log"]
        self.movie_data_extensions = [".pst", ".ps"]

    def get_animal_tag_raw_data_mapping(self, files_chosen: list) -> dict:

        if len(files_chosen) == 0:
            return {}
        else:
            dict2return = {}
            for fle in files_chosen:

                fle_path = pl.Path(fle)
                dict2return[fle_path.name.split(".")[0]] = [fle]

            return dict2return

    def get_path_relative_to_data_dir(self, fle):

        for extension in self.movie_data_extensions:
            if fle.endswith(extension):
                fle_path = pl.PureWindowsPath(fle)
                possible_dbb1 = str(pl.Path(fle_path.parts[-2]) / fle_path.stem)
                return 1, str(possible_dbb1)

        else:
            return 0, "wrong extension"

    def convert_vws_names_to_lst_names(self, vws_measurement_series, default_row):
        """
        Convert values from vws.log nomenclaure to internal measurement list nomenclature
        :param vws_measurement_series: pandas.Series
        :param default_row: pandas.Series with default values
        :return: pandas.series
        """

        logging.getLogger("VIEW").info(f'Parsing measurement with label {vws_measurement_series["Label"]}')
        lst_line = default_row.copy()
        lst_line['Measu'] = vws_measurement_series['index'] + 1
        lst_line['Label'] = vws_measurement_series['Label']

        expected_data_file = vws_measurement_series["Location"]
        if expected_data_file[-2:] == 'ps':
            # there is one version of the macro in tillVision that "eats" the last t of the file name
            logging.getLogger("VIEW").warning('adding a t to the .ps file name to make it .pst')
            expected_data_file += 't'

        analyze, dbb1_relative = self.get_path_relative_to_data_dir(expected_data_file)
        if analyze == 0:
            logging.getLogger("VIEW").warning(
                f"Data file {expected_data_file} not found! Setting analyze=0 for this measurement")
        lst_line['DBB1'] = dbb1_relative
        lst_line["Analyze"] = analyze * int(lst_line.get("Analyze", 1))
        lst_line['Cycle'] = vws_measurement_series["dt"]
        lst_line['Lambda'] = vws_measurement_series['MonochromatorWL_nm']
        lst_line['UTC'] = vws_measurement_series['UTCTime']

        return pd.DataFrame(lst_line).T

    def get_mtime(self, utc, first_utc):

        time_since_first_utc = pd.to_timedelta(utc - first_utc, unit="s")
        return str(time_since_first_utc).split(" days ")[1]


class TillImporterOneWavelength(TillImporter):

    def __init__(self, default_values: typing.Mapping):

        super().__init__(default_values)
        self.LE_loadExp = 3

    # for till data, a single raw data file is a vws.log file
    def read_single_measurement_metadata(self, fle: str, fle_ind: int,
                                         measurement_filter: typing.Callable[[pd.Series], bool]) -> pd.DataFrame:
        vws_manager = VWSDataManager(fle)
        measurements = vws_manager.get_all_metadata(filter=measurement_filter,
                                                    additional_cols_func=additional_cols_func)
        first_utc = vws_manager.get_earliest_utc()
        this_lst_frame = pd.DataFrame()

        if len(measurements) == 0:
            logging.getLogger("VIEW").warning(
                f"In {fle}: No usable measurements found for given 'measurement_filter' function")

        for measurement_index, measurement_row in measurements.iterrows():
            lst_line = self.convert_vws_names_to_lst_names(vws_measurement_series=measurement_row,
                                                           default_row=self.get_default_row(),
                                                           )
            lst_line["MTime"] = self.get_mtime(utc=lst_line["UTC"][0], first_utc=first_utc)
            this_lst_frame = this_lst_frame.append(lst_line, ignore_index=True)

        return this_lst_frame


class TillImporterTwoWavelength(TillImporter):

    def __init__(self, default_values: typing.Mapping):

        super().__init__(default_values)
        self.LE_loadExp = 4

    def read_single_measurement_metadata(self, fle: str, fle_ind: int,
                                         measurement_filter: typing.Callable[[pd.Series], bool]) -> pd.DataFrame:

        vws_manager = VWSDataManager(fle)
        measurements_wl340_df, measurements_wl380_df \
            = vws_manager.get_metadata_two_wavelengths(wavelengths=(340, 380), filter=measurement_filter,
                                                       additional_cols_func=additional_cols_func)
        first_utc = vws_manager.get_earliest_utc()
        this_lst_frame = pd.DataFrame()

        for (ind1, measurement_wl340), (ind2, measurement_wl380) in zip(measurements_wl340_df.iterrows(),
                                                                        measurements_wl380_df.iterrows()):
            lst_line_wl340 = self.convert_vws_names_to_lst_names(measurement_wl340, self.get_default_row())
            lst_line_wl380 = self.convert_vws_names_to_lst_names(measurement_wl380, self.get_default_row())
            lst_line_wl340["dbb2"] = lst_line_wl380["DBB1"]
            lst_line_wl340["MTime"] = self.get_mtime(utc=lst_line_wl340["UTC"][0], first_utc=first_utc)
            lst_line_wl380["Analyze"] = 0
            lst_line_wl380["MTime"] = self.get_mtime(utc=lst_line_wl380["UTC"][0], first_utc=first_utc)

            this_lst_frame = this_lst_frame.append(lst_line_wl340, ignore_index=True)
            this_lst_frame = this_lst_frame.append(lst_line_wl380, ignore_index=True)

        return this_lst_frame


class LSMImporter(BaseImporter):

    def __init__(self, default_values: typing.Mapping):

        super().__init__(default_values)
        self.associate_file_type = "Zeiss LSM files"
        self.associated_extensions = [".lsm"]
        self.movie_data_extensions = [".lsm"]
        self.LE_loadExp = 20

    def get_path_relative_to_data_dir(self, fle):

        for movie_data_extension in self.movie_data_extensions:
            if fle.endswith(movie_data_extension):
                fle_path = pl.PureWindowsPath(fle)
                return 1, str(pl.Path(fle_path.parts[-3]) / fle_path.parts[-2] / fle_path.stem)
        else:
            return 0, -1

    def convert_lsm_metadata_to_lst_row(self, measu, fle, lsm_metadata, default_row):
        """
        Convert values from lsm_metadata to .lst nomenclature
        :param lsm_metadata: dict, like the one returned by tifffile.TiffFile.lsm_metadata
        :param default_row: pandas.Series, with default values
        :return: pandas.Series
        """

        lst_line = default_row.copy()
        lst_line["Label"] = lsm_metadata["ScanInformation"]["Name"]
        # converting from seconds to milliseconds
        lst_line["Cycle"] = lsm_metadata["TimeIntervall"] * 1000
        lst_line["Lambda"] = lsm_metadata["ScanInformation"]["Tracks"][0]["IlluminationChannels"][0]["Wavelength"]
        lst_line['UTC'] = excel_datetime(lsm_metadata["ScanInformation"]["Sample0time"]).timestamp()
        # convert from meters to micrometers
        lst_line["PxSzX"] = lsm_metadata["VoxelSizeX"] / 1e-6
        lst_line["PxSzY"] = lsm_metadata["VoxelSizeY"] / 1e-6

        analyze, dbb1_relative = self.get_path_relative_to_data_dir(fle)
        lst_line["DBB1"] = dbb1_relative
        lst_line["Analyze"] = analyze
        lst_line["Measu"] = measu

        return pd.DataFrame(lst_line).T

    # for till data, a single raw data file is a .lsm file
    def read_single_measurement_metadata(self, fle: str, fle_ind: int,
                                         measurement_filter: typing.Callable[[pd.Series], bool] = True) -> pd.DataFrame:

        lsm_metadata = tifffile.TiffFile(fle).lsm_metadata

        lst_row = self.convert_lsm_metadata_to_lst_row(measu=fle_ind + 1,
                                                       fle=fle,
                                                       lsm_metadata=lsm_metadata,
                                                       default_row=self.get_default_row())

        return lst_row

    def get_animal_tag_raw_data_mapping(self, files_chosen: list) -> dict:

        if len(files_chosen) == 0:
            return {}
        else:
            parents = [pl.Path(fle).parent for fle in files_chosen]
            assert all(x == parents[0] for x in parents), f"LSM files specified for constructing measurement " \
                                                          f"list file do no belong to the same directory: " \
                                                          f"{files_chosen}"
            return {parents[0].parent.name: files_chosen}


def get_importer_class(LE_loadExp):

    if LE_loadExp == 3:

        return TillImporterOneWavelength

    elif LE_loadExp == 4:

        return TillImporterTwoWavelength

    elif LE_loadExp == 20:

        return LSMImporter

    else:

        raise NotImplementedError


def get_setup_extension(LE_loadExp):

    importer_class = get_importer_class(LE_loadExp)
    return importer_class({}).movie_data_extension
