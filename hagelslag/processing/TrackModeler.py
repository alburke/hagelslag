import numpy as np
import pandas as pd
import cPickle
import json
import os
from copy import deepcopy
from glob import glob
from multiprocessing import Pool


class TrackModeler(object):
    """
    TrackModeler is designed to load and process data generated by TrackProcessing and then use that data to fit
    machine learning models to predict whether or not hail will occur, hail size, and translation errors in time and
    space.
    """
    def __init__(self,
                 ensemble_name,
                 train_data_path,
                 forecast_data_path,
                 member_files,
                 start_dates,
                 end_dates,
                 group_col="Microphysics"):
        self.train_data_path = train_data_path
        self.forecast_data_path = forecast_data_path
        self.ensemble_name = ensemble_name
        self.member_files = member_files
        self.start_dates = start_dates
        self.end_dates = end_dates
        self.data = {"train": {}, "forecast": {}}
        self.condition_models = {}
        self.size_models = {}
        self.size_distribution_models = {}
        self.track_models = {"translation-x": {},
                             "translation-y": {},
                             "start-time": {}}
        self.group_col = group_col
        return

    def load_data(self, mode="train", format="csv"):
        """
        Load data from flat data files containing total track information and information about each timestep.
        The two sets are combined using merge operations on the Track IDs. Additional member information is gathered
        from the appropriate member file.

        Args
            mode: str
                "train" or "forecast"
            format: str
                file format being used. Default is "csv"
        """
        if mode in self.data.keys():
            run_dates = pd.DatetimeIndex(start=self.start_dates[mode],
                                        end=self.end_dates[mode],
                                        freq="1D")
            run_date_str = [d.strftime("%Y%m%d") for d in run_dates.date]
            print(run_date_str)
            all_total_track_files = sorted(glob(getattr(self, mode + "_data_path") + \
                                            "*total_" + self.ensemble_name + "*." + format))
            all_step_track_files = sorted(glob(getattr(self, mode + "_data_path") + \
                                           "*step_" + self.ensemble_name + "*." + format))
            total_track_files = []
            for track_file in all_total_track_files:
                file_date = track_file.split("_")[-1][:-4]
                if file_date in run_date_str:
                    total_track_files.append(track_file)
            step_track_files = []
            for step_file in all_step_track_files:
                file_date = step_file.split("_")[-1][:-4]
                if file_date in run_date_str:
                    step_track_files.append(step_file)            
            self.data[mode]["total"] = pd.concat(map(pd.read_csv, total_track_files),
                                                 ignore_index=True)
            self.data[mode]["total"] = self.data[mode]["total"].fillna(0)
            self.data[mode]["total"] = self.data[mode]["total"].replace([np.inf, -np.inf], 0)
            self.data[mode]["step"] = pd.concat(map(pd.read_csv, step_track_files),
                                                ignore_index=True).fillna(0)
            self.data[mode]["step"] = self.data[mode]["step"].replace([np.inf, -np.inf], 0)
            if mode == "forecast":
                self.data[mode]["step"] = self.data[mode]["step"].drop_duplicates("Step_ID")
            self.data[mode]["member"] = pd.read_csv(self.member_files[mode])
            self.data[mode]["combo"] = pd.merge(self.data[mode]["step"],
                                                self.data[mode]["total"],
                                                on="Track_ID",
                                                suffixes=("_Step", "_Total"))
            self.data[mode]["combo"] = pd.merge(self.data[mode]["combo"],
                                                self.data[mode]["member"],
                                                on="Ensemble_Member")
            self.data[mode]["total_group"] = pd.merge(self.data[mode]["total"],
                                                      self.data[mode]["member"],
                                                      on="Ensemble_Member")

    def calc_copulas(self,
                     output_file,
                     model_names=("start-time", "translation-x", "translation-y"),
                     label_columns=("Start_Time_Error", "Translation_Error_X", "Translation_Error_Y")):
        """
        Calculate a copula multivariate normal distribution from the training data for each group of ensemble members.
        Distributions are written to a pickle file for later use.

        Args:
            output_file: str
                Pickle file
            model_names:
                Names of the tracking models
            label_columns:
                Names of the data columns used for labeling

        Returns:

        """
        if len(self.data['train']) == 0:
            self.load_data()
        groups = self.data["train"]["member"][self.group_col].unique()
        copulas = {}
        label_columns = list(label_columns)
        for group in groups:
            print group
            group_data = self.data["train"]["total_group"].loc[
                self.data["train"]["total_group"][self.group_col] == group]
            group_data = group_data.dropna()
            group_data.reset_index(drop=True, inplace=True)
            copulas[group] = {}
            copulas[group]["mean"] = group_data[label_columns].mean(axis=0).values
            copulas[group]["cov"] = np.cov(group_data[label_columns].values.T)
            copulas[group]["model_names"] = list(model_names)
            del group_data
        cPickle.dump(copulas, open(output_file, "w"), cPickle.HIGHEST_PROTOCOL)

    def fit_condition_models(self, model_names,
                             model_objs,
                             input_columns,
                             output_column="Hail_Size",
                             output_threshold=0.0):
        """
        Fit machine learning models to predict whether or not hail will occur.

        Args:
            model_names: list
                List of strings with the names for the particular machine learning models
            model_objs: list
                scikit-learn style machine learning model objects.
            input_columns: list
                list of the names of the columns used as input for the machine learning model
            output_column: str
                name of the column used for labeling whether or not the event occurs
            output_threshold: float
                splitting threshold to determine if event has occurred. Default 0.0
        Returns:
        """
        print("Fitting condition models")
        groups = self.data["train"]["member"][self.group_col].unique()
        for group in groups:
            print(group)
            group_data = self.data["train"]["combo"].loc[self.data["train"]["combo"][self.group_col] == group]
            output_data = np.where(group_data[output_column] > output_threshold, 1, 0)
            print("Ones: ", np.count_nonzero(output_data > 0), "Zeros: ", np.count_nonzero(output_data == 0))
            self.condition_models[group] = {}
            for m, model_name in enumerate(model_names):
                print(model_name)
                self.condition_models[group][model_name] = deepcopy(model_objs[m])
                self.condition_models[group][model_name].fit(group_data[input_columns], output_data)
                if hasattr(self.condition_models[group][model_name], "best_estimator_"):
                    print(self.condition_models[group][model_name].best_estimator_) 
                    print(self.condition_models[group][model_name].best_score_)

    def predict_condition_models(self, model_names,
                                 input_columns,
                                 metadata_cols,
                                 data_mode="forecast",
                                 ):
        """
        Apply condition models to forecast data.

        :param model_names:
        :param input_columns:
        :param metadata_cols:
        :param data_mode:
        :return:
        """
        groups = self.condition_models.keys()
        predictions = {}
        for group in groups:
            group_data = self.data[data_mode]["combo"].loc[self.data[data_mode]["combo"][self.group_col] == group]
            if group_data.shape[0] > 0:
                predictions[group] = group_data[metadata_cols]
                for m, model_name in enumerate(model_names):
                    predictions[group].loc[:, model_name] = self.condition_models[group][model_name].predict_proba(
                        group_data.loc[:, input_columns])[:, 1]
        return predictions

    def fit_size_distribution_models(self, model_names, model_objs, input_columns,
                                     output_columns=None):
        if output_columns is None:
            output_columns = ["Shape", "Location", "Scale"]
        groups = np.unique(self.data["train"]["member"][self.group_col])
        for group in groups:
            group_data = self.data["train"]["combo"].loc[self.data["train"]["combo"][self.group_col] == group]
            group_data.dropna(inplace=True)
            group_data = group_data[group_data[output_columns[-1]] > 0]
            self.size_distribution_models[group] = {"multi": {}, "lognorm": {}}
            log_labels = np.log(group_data[output_columns].values)
            log_means = log_labels.mean(axis=0)
            log_sds = log_labels.std(axis=0)
            self.size_distribution_models[group]['lognorm']['mean'] = log_means
            self.size_distribution_models[group]['lognorm']['sd'] = log_sds
            for m, model_name in enumerate(model_names):
                print group, model_name
                self.size_distribution_models[group]["multi"][model_name] = deepcopy(model_objs[m])
                self.size_distribution_models[group]["multi"][model_name].fit(group_data[input_columns],
                                                                              (log_labels - log_means) / log_sds)

    def predict_size_distribution_models(self, model_names, input_columns, metadata_cols, 
                                         data_mode="forecast", location=6):
        groups = self.size_distribution_models.keys()
        predictions = {}
        for group in groups:
            predictions[group] = {}
            group_data = self.data[data_mode]["combo"].loc[self.data[data_mode]["combo"][self.group_col] == group]
            if group_data.shape[0] > 0:
                log_mean = self.size_distribution_models[group]["lognorm"]["mean"]
                log_sd = self.size_distribution_models[group]["lognorm"]["sd"]
                for m, model_name in enumerate(model_names):
                    predictions[group][model_name] = group_data[metadata_cols]
                    multi_predictions = self.size_distribution_models[group]["multi"][model_name].predict(
                        group_data[input_columns])
                    multi_predictions = np.exp(multi_predictions * log_sd + log_mean)
                    if multi_predictions.shape[1] == 2:
                        multi_predictions_temp = np.zeros((multi_predictions.shape[0], 3))
                        multi_predictions_temp[:, 0] = multi_predictions[:, 0]
                        multi_predictions_temp[:, 1] = location
                        multi_predictions_temp[:, 2] = multi_predictions[:, 1]
                        multi_predictions = multi_predictions_temp
                    for p, pred_col in enumerate(["shape", "location", "scale"]):
                        predictions[group][model_name].loc[:, model_name.replace(" ", "-") + "_" + pred_col] = \
                            multi_predictions[:, p]
        return predictions

    def fit_size_models(self, model_names,
                        model_objs,
                        input_columns,
                        output_column="Hail_Size",
                        output_start=5,
                        output_step=5,
                        output_stop=100):
        """
        Fit size models to produce discrete pdfs of forecast hail sizes.

        :param model_names:
        :param model_objs:
        :param input_columns:
        :param output_column:
        :param output_start:
        :param output_step:
        :param output_stop:
        :return:
        """
        print("Fitting size models")
        groups = self.data["train"]["member"][self.group_col].unique()
        output_start = int(output_start)
        output_step = int(output_step)
        output_stop = int(output_stop)
        for group in groups:
            group_data = self.data["train"]["combo"].loc[self.data["train"]["combo"][self.group_col] == group]
            group_data.dropna(inplace=True)
            group_data = group_data[group_data[output_column] >= output_start]
            output_data = group_data[output_column].values.astype(int)
            output_data[output_data > output_stop] = output_stop
            discrete_data = ((output_data - output_start) // output_step) * output_step + output_start
            self.size_models[group] = {}
            self.size_models[group]["outputvalues"] = np.arange(output_start, output_stop + output_step, output_step,
                                                                dtype=int)
            for m, model_name in enumerate(model_names):
                print("{0} {1}".format(group, model_name))
                self.size_models[group][model_name] = deepcopy(model_objs[m])
                self.size_models[group][model_name].fit(group_data[input_columns], discrete_data)

    def predict_size_models(self, model_names,
                            input_columns,
                            metadata_cols,
                            data_mode="forecast"):
        """
        Apply size models to forecast data.

        :param model_names:
        :param input_columns:
        :param metadata_cols:
        :param data_mode:
        :return:
        """
        groups = self.size_models.keys()
        predictions = {}
        for group in groups:
            group_data = self.data[data_mode]["combo"].loc[self.data[data_mode]["combo"][self.group_col] == group]
            if group_data.shape[0] > 0:
                predictions[group] = {}
                output_values = self.size_models[group]["outputvalues"].astype(int)
                for m, model_name in enumerate(model_names):
                    print("{0} {1}".format(group, model_name))
                    pred_col_names = [model_name.replace(" ", "-") + "_{0:02d}".format(p) for p in output_values]
                    predictions[group][model_name] = group_data[metadata_cols]
                    pred_vals = self.size_models[group][model_name].predict_proba(group_data[input_columns])
                    pred_classes = self.size_models[group][model_name].classes_
                    pred_pdf = np.zeros((pred_vals.shape[0], output_values.size))
                    for pcv, pc in enumerate(pred_classes):
                        idx = np.where(output_values == pc)[0][0]
                        pred_pdf[:, idx] = pred_vals[:, pcv]
                    for pcn, pred_col_name in enumerate(pred_col_names):
                        predictions[group][model_name].loc[:, pred_col_name] = pred_pdf[:, pcn]
        return predictions

    def fit_track_models(self,
                         model_names,
                         model_objs,
                         input_columns,
                         output_columns,
                         output_ranges,
                         ):
        """
        Fit machine learning models to predict track error offsets.

        :param model_names:
        :param model_objs:
        :param input_columns:
        :param output_columns:
        :param output_ranges:
        :return:
        """
        print("Fitting track models")
        groups = self.data["train"]["member"][self.group_col].unique()
        for group in groups:
            group_data = self.data["train"]["combo"].loc[self.data["train"]["combo"][self.group_col] == group]
            group_data = group_data.dropna()
            group_data = group_data.loc[group_data["Duration_Step"] == 1]
            for model_type, model_dict in self.track_models.iteritems():
                model_dict[group] = {}
                output_data = group_data[output_columns[model_type]].values.astype(int)
                output_data[output_data < output_ranges[model_type][0]] = output_ranges[model_type][0]
                output_data[output_data > output_ranges[model_type][1]] = output_ranges[model_type][1]
                discrete_data = (output_data - output_ranges[model_type][0]) // output_ranges[model_type][2] * \
                    output_ranges[model_type][2] + output_ranges[model_type][0]
                model_dict[group]["outputvalues"] = np.arange(output_ranges[model_type][0],
                                                              output_ranges[model_type][1] +
                                                              output_ranges[model_type][2],
                                                              output_ranges[model_type][2])
                for m, model_name in enumerate(model_names):
                    print("{0} {1} {2}".format(group, model_type, model_name))
                    model_dict[group][model_name] = deepcopy(model_objs[m])
                    model_dict[group][model_name].fit(group_data[input_columns], discrete_data)

    def predict_track_models(self, model_names,
                             input_columns,
                             metadata_cols,
                             data_mode="forecast",
                             ):
        """
        Predict track offsets on forecast data.

        :param model_names:
        :param input_columns:
        :param metadata_cols:
        :param data_mode:
        :return:
        """
        predictions = {}
        for model_type, track_model_set in self.track_models.iteritems():
            predictions[model_type] = {}
            groups = track_model_set.keys()
            for group in groups:
                group_data = self.data[data_mode]["combo"].loc[self.data[data_mode]["combo"][self.group_col] == group]
                if group_data.shape[0] > 0:
                    predictions[model_type][group] = {}
                    output_values = track_model_set[group]["outputvalues"].astype(int)
                    for m, model_name in enumerate(model_names):
                        print("{0} {1} {2}".format(group, model_type, model_name))
                        pred_col_names = [model_name.replace(" ", "-") + "_{0:02d}".format(p) for p in output_values]
                        predictions[model_type][group][model_name] = group_data[metadata_cols]
                        pred_vals = track_model_set[group][model_name].predict_proba(group_data[input_columns])
                        pred_classes = track_model_set[group][model_name].classes_
                        pred_pdf = np.zeros((pred_vals.shape[0], output_values.size))
                        for pcv, pc in enumerate(pred_classes):
                            idx = np.where(pc == output_values)[0][0]
                            pred_pdf[:, idx] = pred_vals[:, pcv]
                        for pcn, pred_col_name in enumerate(pred_col_names):
                            predictions[model_type][group][model_name].loc[:, pred_col_name] = pred_pdf[:, pcn]
        return predictions

    def save_models(self, model_path):
        """
        Save machine learning models to pickle files.

        :param model_path:
        :return:
        """
        for group, condition_model_set in self.condition_models.iteritems():
            for model_name, model_obj in condition_model_set.iteritems():
                out_filename = model_path + \
                               "{0}_{1}_condition.pkl".format(group,
                                                              model_name.replace(" ", "-"))
                with open(out_filename, "w") as pickle_file:
                    cPickle.dump(model_obj,
                                 pickle_file,
                                 cPickle.HIGHEST_PROTOCOL)
        for group, size_model_set in self.size_models.iteritems():
            for model_name, model_obj in size_model_set.iteritems():
                out_filename = model_path + \
                               "{0}_{1}_size.pkl".format(group,
                                                         model_name.replace(" ", "-"))
                with open(out_filename, "w") as pickle_file:
                    cPickle.dump(model_obj,
                                 pickle_file,
                                 cPickle.HIGHEST_PROTOCOL)
        for group, dist_model_set in self.size_distribution_models.iteritems():
            for model_type, model_objs in dist_model_set.iteritems():
                for model_name, model_obj in model_objs.iteritems():
                    out_filename = model_path + \
                        "{0}_{1}_{2}_sizedist.pkl".format(group,
                                                          model_name.replace(" ", "-"),
                                                          model_type)
                    with open(out_filename, "w") as pickle_file:
                        cPickle.dump(model_obj,
                                     pickle_file,
                                     cPickle.HIGHEST_PROTOCOL)
        for model_type, track_type_models in self.track_models.iteritems():
            for group, track_model_set in track_type_models.iteritems():
                for model_name, model_obj in track_model_set.iteritems():
                    out_filename = model_path + \
                                   "{0}_{1}_{2}_track.pkl".format(group,
                                                                  model_name.replace(" ", "-"),
                                                                  model_type)
                    with open(out_filename, "w") as pickle_file:
                        cPickle.dump(model_obj,
                                     pickle_file,
                                     cPickle.HIGHEST_PROTOCOL)

        return

    def load_models(self, model_path):
        """
        Load models from pickle files.

        :param model_path:
        :return:
        """
        condition_model_files = sorted(glob(model_path + "*_condition.pkl"))
        if len(condition_model_files) > 0:
            for condition_model_file in condition_model_files:
                model_comps = condition_model_file.split("/")[-1][:-4].split("_")
                if model_comps[0] not in self.condition_models.keys():
                    self.condition_models[model_comps[0]] = {}
                model_name = model_comps[1].replace("-", " ")
                with open(condition_model_file) as cmf:
                    self.condition_models[model_comps[0]][model_name] = cPickle.load(cmf)

        size_model_files = sorted(glob(model_path + "*_size.pkl"))
        if len(size_model_files) > 0:
            for size_model_file in size_model_files:
                model_comps = size_model_file.split("/")[-1][:-4].split("_")
                if model_comps[0] not in self.size_models.keys():
                    self.size_models[model_comps[0]] = {}
                model_name = model_comps[1].replace("-", " ")
                with open(size_model_file) as smf:
                    self.size_models[model_comps[0]][model_name] = cPickle.load(smf)

        size_dist_model_files = sorted(glob(model_path + "*_sizedist.pkl"))
        if len(size_dist_model_files) > 0:
            for dist_model_file in size_dist_model_files:
                model_comps = dist_model_file.split("/")[-1][:-4].split("_")
                if model_comps[0] not in self.size_distribution_models.keys():
                    self.size_distribution_models[model_comps[0]] = {}
                if model_comps[2] not in self.size_distribution_models[model_comps[0]].keys():
                    self.size_distribution_models[model_comps[0]][model_comps[2]] = {}
                model_name = model_comps[1].replace("-", " ")
                with open(dist_model_file) as dmf:
                    self.size_distribution_models[model_comps[0]][model_comps[2]][model_name] = cPickle.load(dmf)

        track_model_files = sorted(glob(model_path + "*_track.pkl"))
        if len(track_model_files) > 0:
            for track_model_file in track_model_files:
                model_comps = track_model_file.split("/")[-1][:-4].split("_")
                group = model_comps[0]
                model_name = model_comps[1].replace("-", " ")
                model_type = model_comps[2]
                if model_type not in self.track_models.keys():
                    self.track_models[model_type] = {}
                if group not in self.track_models[model_type].keys():
                    self.track_models[model_type][group] = {}
                with open(track_model_file) as tmf:
                    self.track_models[model_type][group][model_name] = cPickle.load(tmf)

    def output_forecasts_json(self, forecasts,
                              condition_model_names,
                              size_model_names,
                              dist_model_names,
                              track_model_names,
                              json_data_path,
                              out_path):
        """
        Output forecast values to geoJSON file format.

        :param forecasts:
        :param condition_model_names:
        :param size_model_names:
        :param track_model_names:
        :param json_data_path:
        :param out_path:
        :return:
        """
        total_tracks = self.data["forecast"]["total"]
        for r in np.arange(total_tracks.shape[0]):
            track_id = total_tracks.loc[r, "Track_ID"]
            print(track_id)
            track_num = track_id.split("_")[-1]
            ensemble_name = total_tracks.loc[r, "Ensemble_Name"]
            member = total_tracks.loc[r, "Ensemble_Member"]
            group = self.data["forecast"]["member"].loc[self.data["forecast"]["member"]["Ensemble_Member"] == member,
                                                        self.group_col].values[0]
            run_date = track_id.split("_")[-4][:8]
            step_forecasts = {}
            for ml_model in condition_model_names:
                step_forecasts["condition_" + ml_model.replace(" ", "-")] = forecasts["condition"][group].loc[
                    forecasts["condition"][group]["Track_ID"] == track_id, ml_model]
            for ml_model in size_model_names:
                step_forecasts["size_" + ml_model.replace(" ", "-")] = forecasts["size"][group][ml_model].loc[
                    forecasts["size"][group][ml_model]["Track_ID"] == track_id]
            for ml_model in dist_model_names:
                step_forecasts["dist_" + ml_model.replace(" ", "-")] = forecasts["dist"][group][ml_model].loc[
                    forecasts["dist"][group][ml_model]["Track_ID"] == track_id]
            for model_type in forecasts["track"].keys():
                for ml_model in track_model_names:
                    mframe = forecasts["track"][model_type][group][ml_model]
                    step_forecasts[model_type + "_" + ml_model.replace(" ", "-")] = mframe.loc[
                        mframe["Track_ID"] == track_id]
            json_file_name = "{0}_{1}_{2}_model_track_{3}.json".format(ensemble_name,
                                                                       run_date,
                                                                       member,
                                                                       track_num)
            full_json_path = json_data_path + "/".join([run_date, member]) + "/" + json_file_name
            with open(full_json_path) as json_file_obj:
                try:
                    track_obj = json.load(json_file_obj)
                except:
                    print full_json_path + " not found"
                    continue
            for f, feature in enumerate(track_obj['features']):
                del feature['properties']['attributes']
                for model_name, fdata in step_forecasts.iteritems():
                    ml_model_name = model_name.split("_")[1]
                    if "condition" in model_name:
                        feature['properties'][model_name] = fdata.values[f]
                    else:
                        predcols = []
                        for col in fdata.columns:
                            if ml_model_name in col:
                                predcols.append(col)
                        feature['properties'][model_name] = fdata.loc[:, predcols].values[f].tolist()
            full_path = []
            for part in [run_date, member]:
                full_path.append(part)
                if not os.access(out_path + "/".join(full_path), os.R_OK):
                    try:
                        os.mkdir(out_path + "/".join(full_path))
                    except OSError:
                        print "directory already created"
            out_json_filename = out_path + "/".join(full_path) + "/" + json_file_name
            with open(out_json_filename, "w") as out_json_obj:
                json.dump(track_obj, out_json_obj, indent=1, sort_keys=True)
        return

    def output_forecasts_json_parallel(self, forecasts,
                                      condition_model_names,
                                      size_model_names,
                                      dist_model_names,
                                      track_model_names,
                                      json_data_path,
                                      out_path,
                                      num_procs):
        pool = Pool(num_procs)
        total_tracks = self.data["forecast"]["total_group"]
        for r in total_tracks.index:
            track_id = total_tracks.loc[r, "Track_ID"]
            print(track_id)
            track_num = track_id.split("_")[-1]
            ensemble_name = total_tracks.loc[r, "Ensemble_Name"]
            member = total_tracks.loc[r, "Ensemble_Member"]
            group = total_tracks.loc[r, self.group_col]
            run_date = track_id.split("_")[-4][:8]
            step_forecasts = {}
            for ml_model in condition_model_names:
                step_forecasts["condition_" + ml_model.replace(" ", "-")] = forecasts["condition"][group].loc[
                    forecasts["condition"][group]["Track_ID"] == track_id, ml_model]
            for ml_model in size_model_names:
                step_forecasts["size_" + ml_model.replace(" ", "-")] = forecasts["size"][group][ml_model].loc[
                    forecasts["size"][group][ml_model]["Track_ID"] == track_id]
            for ml_model in dist_model_names:
                step_forecasts["dist_" + ml_model.replace(" ", "-")] = forecasts["dist"][group][ml_model].loc[
                    forecasts["dist"][group][ml_model]["Track_ID"] == track_id]
            for model_type in forecasts["track"].keys():
                for ml_model in track_model_names:
                    mframe = forecasts["track"][model_type][group][ml_model]
                    step_forecasts[model_type + "_" + ml_model.replace(" ", "-")] = mframe.loc[
                        mframe["Track_ID"] == track_id]
            pool.apply_async(output_forecast, (step_forecasts, run_date, ensemble_name, member, track_num, json_data_path,
                                               out_path))
        pool.close()
        pool.join()
        return


def output_forecast(step_forecasts, run_date, ensemble_name, member, track_num, json_data_path, out_path):
    json_file_name = "{0}_{1}_{2}_model_track_{3}.json".format(ensemble_name,
                                                               run_date,
                                                               member,
                                                               track_num)
    full_json_path = json_data_path + "/".join([run_date, member]) + "/" + json_file_name
    try:
        json_file_obj = open(full_json_path)
        track_obj = json.load(json_file_obj)
        json_file_obj.close()
    except IOError:
        print(full_json_path + " not found")
        return
    for f, feature in enumerate(track_obj['features']):
        del feature['properties']['attributes']
        for model_name, fdata in step_forecasts.iteritems():
            ml_model_name = model_name.split("_")[1]
            if "condition" in model_name:
                feature['properties'][model_name] = fdata.values[f]
            else:
                predcols = []
                for col in fdata.columns:
                    if ml_model_name in col:
                        predcols.append(col)
                feature['properties'][model_name] = fdata.loc[:, predcols].values[f].tolist()
    full_path = []
    for part in [run_date, member]:
        full_path.append(part)
        if not os.access(out_path + "/".join(full_path), os.R_OK):
            try:
                os.mkdir(out_path + "/".join(full_path))
            except OSError:
                print "directory already created"
    out_json_filename = out_path + "/".join(full_path) + "/" + json_file_name
    try:
        out_json_obj = open(out_json_filename, "w")
        json.dump(track_obj, out_json_obj, indent=1, sort_keys=True)
        out_json_obj.close()
    except IOError:
        print(out_json_filename + " not found")
        return
    return
