"""
Model Evaluator

This script contain a set of elements to help the postmodeling evaluation
of audited models by triage.Audition. This will be a continuing list of
routines that can be scaled up and grow according to the needs of the
project, or other postmodeling approaches.

To run most of this routines you will need:
    - S3 credentials (if used) or specify the path to both feature and
    prediction matrices.
    - Working database conn.
"""

import pandas as pd
import numpy as np
import yaml
import pickle
import graphviz
import collections
from functools import partial
from sqlalchemy.sql import text
from matplotlib import pyplot as plt
from descriptors import cachedproperty
from sklearn.ensemble import RandomForestClassifier
from sklearn import metrics
from sklearn import tree
from collections import namedtuple
from triage.component.catwalk.storage import ProjectStorage, ModelStorageEngine, MatrixStorageEngine
from parameters import ThresholdIterator, PostmodelParameters
from utils.aux_funcs import *


class ModelEvaluator(object):
    '''
    ModelExtractor class calls the model metadata from the database
    and hold model_id metadata features on each of the class attibutes.
    This class will contain any information about the model, and will be
    used to make comparisons and calculations across models.

    A pair of (model_group_id, model_id) is needed to instate the class. These
    can be feeded from the get models_ids.
    '''
    def __init__(self, model_group_id, model_id):
        self.model_id = model_id
        self.model_group_id = model_group_id

        # Retrive model_id metadata from the model_metadata schema
        model_metadata = pd.read_sql(
        f'''WITH
        individual_model_ids_metadata AS(
        SELECT m.model_id,
               m.model_group_id,
               m.hyperparameters,
               m.model_hash,
               m.experiment_hash,
               m.train_end_time,
               m.train_matrix_uuid,
               m.training_label_timespan,
               mg.model_type,
               mg.model_config
            FROM model_metadata.models m
            JOIN model_metadata.model_groups mg
            USING (model_group_id)
            WHERE model_group_id = {self.model_group_id}
            AND model_id = {self.model_id}
        ),
        individual_model_id_matrices AS(
        SELECT DISTINCT ON (matrix_uuid)
               model_id,
               matrix_uuid
            FROM test_results.predictions
            WHERE model_id = ANY(
                SELECT model_id
                FROM individual_model_ids_metadata
            )
        )
        SELECT metadata.*, test.*
        FROM individual_model_ids_metadata AS metadata
        LEFT JOIN individual_model_id_matrices AS test
        USING(model_id);''', con=conn)

        # Add metadata attributes to model
        self.model_type = model_metadata.loc[0, 'model_type']
        self.hyperparameters = model_metadata.loc[0, 'hyperparameters']
        self.model_hash = model_metadata.loc[0, 'model_hash']
        self.train_matrix_uuid = model_metadata.loc[0, 'train_matrix_uuid']
        self.pred_matrix_uuid = model_metadata.loc[0, 'matrix_uuid']
        self.as_of_date = model_metadata.loc[0, 'train_end_time']

    def __repr__(self):
        return (
        f'Model object for model_id: {self.model_id}\n'
        f'Model Group: {self.model_group_id}\n'
        f'Model type: {self.model_type}\n'
        f'Train End Time: {self.train_end_time}\n'
        f'Model hyperparameters: {self.hyperparameters}\n'
        f'''Matrix hashes (train,test): [{self.train_matrix_uuid},
                                        {self.pred_matrix_uuid}]'''
        )

    @cachedproperty
    def predictions(self):
        preds = pd.read_sql(
            f'''
            SELECT model_id,
                   entity_id,
                   as_of_date,
                   score,
                   label_value,
                   COALESCE(rank_abs, RANK() OVER(ORDER BY score DESC)) AS rank_abs,
                   COALESCE(rank_pct, percent_rank() OVER(ORDER BY score DESC)) * 100 as rank_pct, 
                   test_label_timespan
            FROM test_results.predictions
            WHERE model_id = {self.model_id}
            AND label_value IS NOT NULL
            ''', con=conn)

        return preds

    @cachedproperty
    def feature_importances(self):
        features = pd.read_sql(
            f'''
            SELECT model_id,
                   feature,
                   feature_importance,
                   rank_abs
            FROM train_results.feature_importances
            WHERE model_id = {self.model_id}
            ''', con=conn)
        return features

    @cachedproperty
    def metrics(self):
        model_metrics = pd.read_sql(
            f'''
            SELECT model_id,
                   metric,
                   parameter,
                   value,
                   num_labeled_examples,
                   num_labeled_above_threshold,
                   num_positive_labels
            FROM test_results.evaluations
            WHERE model_id = {self.model_id}
            ''', con=conn)
        return model_metrics

    @cachedproperty
    def crosstabs(self):
        model_crosstabs = pd.read_sql(
            f'''
            SELECT model_id,
                   as_of_date,
                   metric,
                   feature_column,
                   value,
                   threshold_unit,
                   threshold_value
            FROM test_results.crosstabs_test
            WHERE model_id = {self.model_id}
            ''', con=conn)

        return model_crosstabs
    
    def preds_matrix(self, path):
        '''
        Load predictions matrices using the catwalk.storage.ProjectStorage.
        This class allow the user to define a project path that can be either a
        system local path or an s3 path and it will handle itself this
        different infrastructures. 

        Once defined, we can pass this object to the MatrixStorageEngine that
        will read this object and return a MatrixSotre object with a set of
        handy methods to handle matrices

        Arguments:
            path: project path to initiate the ProjectStorage object
        '''
        cache = self.__dict__.setdefault('_preds_matrix_cache', {})
        try:
            return cache[path]
        except KeyError:
            pass

        storage = ProjectStorage(path)
        matrix_storage = MatrixStorageEngine(storage).get_store(self.pred_matrix_uuid)
        mat = matrix_storage.matrix

        # Merge with predictions table and return complete matrix
        merged_df = mat.merge(self.predictions,
                             on='entity_id',
                             how='inner',
                             suffixes=('test', 'pred'))

        cache[path] = merged_df

    def train_matrix(self, path):
        '''
        Load predictions matrices using the catwalk.storage.ProjectStorage.
        This class allow the user to define a project path that can be either a
        system local path or an s3 path and it will handle itself this
        different infrastructures. 

        Once defined, we can pass this object to the MatrixStorageEngine that
        will read this object and return a MatrixSotre object with a set of
        handy methods to handle matrices

        Arguments:
            path: project path to initiate the ProjectStorage object
        '''
        cache = self.__dict__.setdefault('_preds_matrix_cache', {})
        try:
            return cache[path]
        except KeyError:
            pass

        storage = ProjectStorage(path)
        matrix_storage = MatrixStorageEngine(storage).get_store(self.train_matrix_uuid)
        mat = matrix_storage.matrix

        # Merge with predictions table and return complete matrix
        merged_df = mat.merge(self.predictions,
                             on='entity_id',
                             how='inner',
                             suffixes=('test', 'pred'))

        cache[path] = mat

    def plot_score_distribution(self,
                               save_file=False,
                               name_file=None,
                               figsize=(16,12),
                               fontsize=20):
        '''
        Generate an histograms with the raw distribution of the predicted
        scores for all entities. 
            - Arguments:
                - save_file (bool): save file to disk as png. Default is False.
                - name_file (string): specify name file for saved plot.
                - label_names(tuple): define custom label names for class.
                - figsize (tuple): specify size of plot. 
                - fontsize (int): define custom fontsize. 20 is set by default.
 
        '''

        df_ = self.predictions.filter(items=['score']) 

        fig, ax = plt.subplots(1, figsize=figsize)
        plt.hist(df_.score,
                 bins=20,
                 normed=True,
                 alpha=0.5,
                 color='blue')
        plt.axvline(df_.score.mean(),
                    color='black',
                    linestyle='dashed')
        ax.set_ylabel('Frequency', fontsize=fontsize)
        ax.set_xlabel('Score', fontsize=fontsize)
        plt.title('Score Distribution', y =1.2, 
                  fontsize=fontsize)
        plt.show()

        if save_file:
            plt.savefig(str(name_file + '.png'))

    def plot_score_label_distributions(self, 
                                       save_file=False, 
                                       name_file=None,
                                       label_names = ('Label = 0', 'Label = 1'),
                                       figsize=(16, 12),
                                       fontsize=20):
        '''
        Generate a histogram showing the label distribution for predicted
        entities:
            - Arguments:
                - save_file (bool): save file to disk as png. Default is False.
                - name_file (string): specify name file for saved plot.
                - label_names(tuple): define custom label names for class.
                - figsize (tuple): specify size of plot. 
                - fontsize (int): define custom fontsize. 20 is set by default.
        '''

        df_predictions = self.predictions.filter(items=['score', 'label_value'])
        df__0 = df_predictions[df_predictions.label_value == 0]
        df__1 = df_predictions[df_predictions.label_value == 1]

        fig, ax = plt.subplots(1, figsize=figsize)
        plt.hist(df__0.score,
                 bins=20,
                 normed=True,
                 alpha=0.5,
                 color='skyblue',
                 label=label_names[0])
        plt.hist(list(df__1.score),
                 bins=20,
                 normed=True,
                 alpha=0.5,
                 color='orange',
                 label=label_names[1])
        plt.axvline(df__0.score.mean(),
                    color='skyblue',
                    linestyle='dashed')
        plt.axvline(df__1.score.mean(),
                    color='orange',
                    linestyle='dashed')
        plt.legend(bbox_to_anchor=(0., 1.005, 1., .102),
                   loc=8,
                   ncol=2,
                   borderaxespad=0.,
                   fontsize=fontsize-2)
        ax.set_ylabel('Frequency', fontsize=fontsize)
        ax.set_xlabel('Score', fontsize=fontsize)
        plt.title('Score Distribution across Labels', y =1.2, 
                  fontsize=fontsize)
        plt.show()

        if save_file:
            plt.savefig(str(name_file + '.png'))

    def plot_feature_importances(self,
                                 save_file=False,
                                 name_file=None,
                                 n_features_plots=30,
                                 figsize=(16, 12),
                                 fontsize=20):
        '''
        Generate a bar chart of the top n feature importances (by absolute value)
        Arguments:
                - save_file (bool): save file to disk as png. Default is False.
                - name_file (string): specify name file for saved plot.
                - n_features_plots (int): number of top features to plot
                - figsize (tuple): figure size to pass to matplotlib
                - fontsize (int): define custom fontsize for labels and legends.
        '''

        importances = self.feature_importances.filter(items=['feature', 'feature_importance'])
        importances = importances.set_index('feature')

        # Sort by the absolute value of the importance of the feature
        importances['sort'] = abs(importances['feature_importance'])
        importances = importances.sort_values(by='sort', ascending=False).drop('sort', axis=1)
        importances = importances[0:n_features_plots]

        # Show the most important positive feature at the top of the graph
        importances = importances.sort_values(by='feature_importance', ascending=True)

        fig, ax = plt.subplots(figsize=figsize)
        ax.tick_params(labelsize=16)
        importances.plot(kind="barh", legend=False, ax=ax)
        ax.set_frame_on(False)
        ax.set_xlabel('Score', fontsize=20)
        ax.set_ylabel('Feature', fontsize=20)
        plt.tight_layout()
        plt.title('Top {} Feature Importances'.format(n_features_plots), fontsize=fontsize).set_position([.5, 0.99])
        if save_file:
            plt.savefig(str(name_file + '.png'))

    def plot_feature_importances_std_err(self,
                                         path,
                                         save_file=False,
                                         name_file=None,
                                         n_features_plots=30,
                                         figsize=(16,21),
                                         fontsize=20):
        '''
        Generate a bar chart of the top n features importances showing the
        error bars.
        Arguments:
                - save_file (bool): save file to disk as png. Default is False.
                - name_file (string): specify name file for saved plot.
                - n_features_plots (int): number of top features to plot.
                - figsize (tuple): figuresize to pass to matplotlib.
                - fontsize (int): define a custom fontsize for labels and legends.
                - *path: path to retrieve model pickle
        '''
        storage = ProjectStorage(path)
        model_object = ModelStorageEngine(storage).load(self.model_hash)
        matrix_object = MatrixStorageEngine(storage).get_store(self.pred_matrix_uuid)

        # Calculate errors from model
        importances = model_object.feature_importances_
        std = np.std([tree.feature_importances_ for tree in model_object.estimators_],
                    axis=0)
        # Create dataframe and sort select the most relevant features (defined
        # by n_features_plot) 
        importances_df = pd.DataFrame({
            'feature_name': matrix_object.columns(),
            'std': std,
            'feature_importance': importances
        }).set_index('feature_name')

        importances_sort = importances_df.sort_values(['feature_importance'],
                                                       ascending=False)
        importances_filter = importances_sort[:n_features_plots]
 
        # Plot features in order
        importances_ordered = importances_filter.sort_values(['feature_importance'], ascending=True)

        # Plot features with sd bars
        fig, ax = plt.subplots(figsize=figsize)
        ax.tick_params(labelsize=16)
        importances_ordered['feature_importance'].plot.barh(legend=False, ax=ax,
                                               xerr=importances_ordered['std'])
        ax.set_frame_on(False)
        ax.set_xlabel('Score', fontsize=20)
        ax.set_ylabel('Feature', fontsize=20)
        plt.tight_layout()
        plt.title(f'Top {n_features_plots} Feature Importances with SD',
                  fontsize=fontsize).set_position([.5, 0.99])

        if save_file:
            plt.savefig(str(name_file + '.png'))

    def compute_AUC(self):
        '''
        Utility function to generate ROC and AUC data to plot ROC curve
        Returns (tuple): 
            - (false positive rate, true positive rate, thresholds, AUC)
        '''

        label_ = self.predictions.label_value
        score_ = self.predictions.score
        fpr, tpr, thresholds = metrics.roc_curve(
            label_, score_, pos_label=1)

        return (fpr, tpr, thresholds, metrics.auc(fpr, tpr))

    def plot_ROC(self, 
                 figsize=(16, 12),
                 fontsize=20):
        '''
        Plot an ROC curve for this model and label it with AUC
        using the sklearn.metrics methods. 
        Arguments:
            - figsize (tuple): figure size to pass to matplotlib
            - fontsize (int): fontsize for title. Default is 20.
        '''

        fpr, tpr, thresholds, auc = self.compute_AUC()
        auc = "%.2f" % auc

        title = 'ROC Curve, AUC = ' + str(auc)
        fig, ax = plt.subplots(figsize=figsize)
        ax.plot(fpr, tpr, "#000099", label='ROC curve')
        ax.plot([0, 1], [0, 1], 'k--', label='Baseline')
        plt.xlim([0.0, 1.0])
        plt.ylim([0.0, 1.05])
        plt.xlabel('False Positive Rate', fontsize=fontsize)
        plt.ylabel('True Positive Rate', fontsize=fontsize)
        plt.legend(loc='lower right')
        plt.title(title, fontsize=fontsize)
        plt.show()

    def plot_recall_fpr_n(self,
                          figsize=(16, 12),
                          fontsize=20):
        '''
        Plot recall and the false positive rate against depth into the list
        (esentially just a deconstructed ROC curve) along with optimal bounds.
        Arguments:
            - figsize (tuple): define a plot size to pass to matplotlib
            - fontsize (int): define a custom font size to labels and axes 
        '''


        _labels = self.predictions.filter(items=['label_value', 'score'])

        # since tpr and recall are different names for the same metric, we can just
        # grab fpr and tpr from the sklearn function for ROC
        fpr, recall, thresholds, auc = self.compute_AUC()

        # turn the thresholds into percent of the list traversed from top to bottom
        pct_above_per_thresh = []
        num_scored = float(len(_labels.score))
        for value in thresholds:
            num_above_thresh = len(_labels.loc[_labels.score >= value, 'score'])
            pct_above_thresh = num_above_thresh / num_scored
            pct_above_per_thresh.append(pct_above_thresh)
        pct_above_per_thresh = np.array(pct_above_per_thresh)

        # plot the false positive rate, along with a dashed line showing the optimal bounds
        # given the proportion of positive labels
        plt.clf()
        fig, ax1 = plt.subplots(figsize=figsize)
        ax1.plot([_labels.label_value.mean(), 1], [0, 1], '--', color='gray')
        ax1.plot([0, _labels.label_value.mean()], [0, 0], '--', color='gray')
        ax1.plot(pct_above_per_thresh, fpr, "#000099")
        plt.title('Deconstructed ROC Curve\nRecall and FPR vs. Depth',fontsize=fontsize)
        ax1.set_xlabel('Proportion of Population', fontsize=fontsize)
        ax1.set_ylabel('False Positive Rate', color="#000099", fontsize=fontsize)
        plt.ylim([0.0, 1.05])

    def plot_precision_recall_n(self,
                                figsize=(16, 12),
                                fontsize=20):
        """
        Plot recall and precision curves against depth into the list.
        """


        _labels = self.predictions.filter(items=['label_value', 'score'])

        y_score = _labels.score
        precision_curve, recall_curve, pr_thresholds = \
            metrics.precision_recall_curve(_labels.label_value, y_score)

        precision_curve = precision_curve[:-1]
        recall_curve = recall_curve[:-1]

        # a little bit of a hack, but ensure we start with a cut-off of 0
        # to extend lines to full [0,1] range of the graph
        pr_thresholds = np.insert(pr_thresholds, 0, 0)
        precision_curve = np.insert(precision_curve, 0, precision_curve[0])
        recall_curve = np.insert(recall_curve, 0, recall_curve[0])

        pct_above_per_thresh = []
        number_scored = len(y_score)
        for value in pr_thresholds:
            num_above_thresh = len(y_score[y_score >= value])
            pct_above_thresh = num_above_thresh / float(number_scored)
            pct_above_per_thresh.append(pct_above_thresh)
        pct_above_per_thresh = np.array(pct_above_per_thresh)

        plt.clf()
        fig, ax1 = plt.subplots(figsize=figsize)
        ax1.plot(pct_above_per_thresh, precision_curve, "#000099")
        ax1.set_xlabel('Proportion of population', fontsize=fontsize)
        ax1.set_ylabel('Precision', color="#000099", fontsize=fontsize)
        plt.ylim([0.0, 1.05])
        ax2 = ax1.twinx()
        ax2.plot(pct_above_per_thresh, recall_curve, "#CC0000")
        ax2.set_ylabel('Recall', color="#CC0000", fontsize=fontsize)
        plt.xlim([0.0, 1.0])
        plt.ylim([0.0, 1.05])
        plt.title("Precision-recall at x-proportion", fontsize=fontsize)
        plt.show()
 
    def _error_labeler(self,
                      path,
                      param=None,
                      param_type=None):
        '''
        Explore the underlying causes of errors using decision trees to explain the
        residuals base on the same feature space used in the model. This
        exploration will get the most relevant features that determine y - y_hat
        distance and may help to understand the outomes of some models.

        This function will label the errors and return two elements relevant to
        model these. First, a feature matrix (X) with all the features used by
        the model. Second, an iterator with different labeled errors: FPR, FRR,
        and the general error. 

        Arguments:
            - param_type: (str) type of parameter to define a threshold. Possible
              values come from triage evaluations: rank_abs, or rank_pct
            - param: (int) value 
            - path: path for the ProjectStorage class object
        '''
        self.preds_matrix(path)
        test_matrix = self.preds_matrix(path)

        if param_type == 'rank_abs':
            # Calculate residuals/errors
            test_matrix_thresh = test_matrix.sort_values(['rank_abs'], ascending=True)
            test_matrix_thresh['above_thresh'] = \
                    np.where(test_matrix_thresh['rank_abs'] <= param, 1, 0)
            test_matrix_thresh['error'] = test_matrix_thresh['label_value'] - test_matrix_thresh['above_thresh']
        elif param_type == 'rank_pct':
            # Calculate residuals/errors
            test_matrix_thresh = test_matrix.sort_values(['rank_pct'], ascending=True)
            test_matrix_thresh['above_thresh'] = \
                    np.where(test_matrix_thresh['rank_pct'] <= param, 1, 0)
            test_matrix_thresh['error'] = test_matrix_thresh['label_value'] - test_matrix_thresh['above_thresh']
        else:
            raise AttributeError('''Error! You have to define a parameter type to
                                 set up a threshold
                                 ''')

        # Define labels using the errors
        # By design the order is FPR and FNR 
        error_class = zip([
            (test_matrix_thresh['label_value'] == 0) &
            (test_matrix_thresh['above_thresh'] == 1),
            (test_matrix_thresh['label_value'] == 1) &
            (test_matrix_thresh['above_thresh'] == 0)],
            ['FPR', 'FNR'])
        
        # Create label iterator
        Y = ((np.where(condition, 1, -1), error_type) for condition, error_type in error_class)

        # Define feature space to model: get the list of feature names
        storage = ProjectStorage(path)
        matrix_storage = MatrixStorageEngine(storage).get_store(self.pred_matrix_uuid)
        feature_columns = matrix_storage.columns()

        # Build error matrix and label vector
        X = test_matrix_thresh[feature_columns]

        return(X, Y)

    def _error_modeler(self,
                      depth=None,
                      view_plots=False,
                      **kwargs):
       '''  
       Model labeled errors (residuals) by the error_labeler (FPR, FNR, and
       general residual) using a RandomForestClassifier. This function will
       yield a plot tree for each of the label numpy arrays return by the
       error_labeler (Y).
       Arguments:
           - depth: max number of tree partitions. This is passed directly to
             the classifier.
           - view_plot: the plot is saved to disk by default, but the
             graphviz.Source also allow to load the object and see it in the
             default OS image renderer
           - **kwargs: more arguments passed to the labeler: param indicating
             the threshold value, param_type indicating the type of threshold,
             and the path to the ProjectStorage. 
       '''

       # Get matrices from the labeler
       X, Y = self._error_labeler(param_type = kwargs['param_type'],
                                  param = kwargs['param'],
                                  path=kwargs['path'])

       # Model tree and output tree plot
       for error_label, error_type in Y:

           dot_path = 'error_analysis_' + \
                      str(error_type) + '_' + \
                      str(self.model_id) + '_' + \
                      str(kwargs['param_type']) + '@'+ \
                      str(kwargs['param']) +  '.gv' 

           clf = tree.DecisionTreeClassifier(max_depth=depth)
           clf_fit = clf.fit(X, error_label)
           tree_viz = tree.export_graphviz(clf_fit,
                                           out_file=None,
                                           feature_names=X.columns.values,
                                           filled=True,
                                           rounded=True,
                                           special_characters=True)
           graph = graphviz.Source(tree_viz)
           graph.render(filename=dot_path,
                        directory='error_analysis',
                        view=view_plots)

           print(dot_path)

    def error_analysis(self, threshold_iterator, **kwargs):
        '''
        Error analysis function for ThresholdIterator objects. This function
        have the same functionality as the _error.modeler method, but its
        implemented for iterators, which can be a case use of this analysis.
        If no iterator object is passed, the function will take the needed
        arguments to run the _error_modeler. 
        Arguments:
            - threshold_iterator: A ThresholdIterator class (see paramters.py)
            -**kwags: other arguments passed to _error_modeler
        '''

        error_modeler = partial(self._error_modeler, 
                               depth = kwargs['depth'],
                               path = kwargs['path'],
                               view_plots = kwargs['view_plots'])

        if isinstance(threshold_iterator, collections.Iterable):
           for threshold_type, threshold in threshold_iterator:
               print(threshold_type, threshold)
               error_modeler(param_type = threshold_type,
                             param = threshold)
        else:
           error_modeler(param_type=kwargs['param_type'],
                         param=kwargs['param'])


    def crosstabs_ratio_plot(self, 
                             n_features_plots=30):
        '''
        Plot to visualize the top-k features with the highest mean ratio. This
        plot will show the biggest quantitative differences between the labeled/predicted
        groups 
        '''
        crosstabs_ratio = self.crosstabs.loc[self.crosstabs.metric ==
                                             'ratio_predicted_positive_over_predicted_negative', :]
        crosstabs_ratio_subset = crosstabs_ratio.sort_values(by=['value'],
                                                             ascending=False)[:n_features_plots]



    def test_feature_diffs(self, feature_type, name_or_prefix, suffix='',
                           score_col='score', 
                           entity_col='entity_id',
                           cut_n=None, cut_frac=None, cut_score=None,
                           nbins=None, 
                           thresh_labels=['Above Threshold', 'Below Threshold'],
                           figsize=(12, 16), 
                           figtitle=None, 
                           xticks_map=None,
                           figfontsize=14, 
                           figtitlesize=16,
                           *path):
        '''
        Look at the differences in a feature across a score cut-off. You can specify one of
        the top n (e.g., cut_n=300), top x% (e.g. cut_frac=0.10 for 10%), or a specific score
        cut-off to use (e.g., cut_score=0.4815). Since our matrices have integer values for all
        features, you'll need to specify the type of feature.

        Arguments:
            feature_type:   one of ['continuous', 'categorical', 'boolean']
                            note that categorical columns are assumed mutually exclusive
            name_or_prefix: the feature (column) name for a continuous or boolean feature, or the
                            prefix for a set of categorical features that span several columns
            suffix:         suffix for categorical features column names (usually a collate aggregate)
            score_col:      name of the column with scores in the dataframe df
            entity_col:     name of the column with the entity identifiers
            cut_n:          top n by score_col to use for the cut-off
            cut_frac:       top fraction (between 0 and 1) by score_col to use for the cut-off
            cut_score:      score cut-off
            nbins:          optional parameter for number of bins in continuous histogram
            tresh_lables:   list of labels for entities above and below the threshold, respectively
            figsize:        figure size tuple to pass through to matplotlib
            figtitle:       optional title for the plot
            xticks_map:     optional mapping for a categorical/boolean variable with more readable names for the values
            figfontsize:    font size to use for the plot
            figtitlesize:   font size to use for the plot title
            *path:     arguments to pass through to fetch_features_and_pred_matrix()
        '''

        if self.pred_matrix is None:
            self.load_features__matrix(*path)

        # sort and select columns we'll need
        if feature_type in ['continuous', 'boolean']:
            df_sorted = self.pred_matrix[[entity_col, score_col, name_or_prefix]].copy()
            df_sorted['sort_random'] = np.random.random(len(df_sorted))
            df_sorted.sort_values([score_col, 'sort_random'], ascending=[False, False], inplace=True)

        # for categoricals, combine columns into one categorical column
        # NOTE: assumes the categoricals are mutually exclusive
        elif feature_type == 'categorical':
            df_sorted = self.pred_matrix[[entity_col, score_col]].copy()
            df_sorted['sort_random'] = np.random.random(len(df_sorted))
            df_sorted.sort_values([score_col, 'sort_random'], ascending=[False, False], inplace=True)
            df_sorted, cat_col = recombine_categorical(df_sorted, self.pred_matrix, name_or_prefix, suffix, entity_col)

        else:
            raise ValueError('feature_type must be one of continuous, boolean, or categorical')

        # calculate the other two cut variables depending on which is specified
        if cut_n:
            cut_score = df_sorted[:cut_n][score_col].min()
            cut_frac = 1.0*cut_n / len(df_sorted)

        elif cut_frac:
            cut_n = int(np.ceil(len(df_sorted)*cut_frac))
            cut_score = df_sorted[:cut_n][score_col].min()

        elif cut_score:
            cut_n = len(df_sorted.loc[df_sorted[score_col] >= cut_score])
            cut_frac = 1.0*cut_n / len(df_sorted)

        else:
            raise ValueError('Must specify one of cut_n, cut_frac, or cut_score')
     # seems like there should be a way to do this without having to touch the index?
        df_sorted.reset_index(inplace=True)
        df_sorted['above_thresh'] = df_sorted.index < cut_n
        df_sorted['above_thresh'] = df_sorted['above_thresh'].map({True: thresh_labels[0], False: thresh_labels[1]})


        # for booleans and categoricals, plot the discrete distributions and calculate chi-squared stats
        if feature_type in ['categorical', 'boolean']:
            if feature_type == 'boolean':
                cat_col = name_or_prefix

            self.categorical_plot_and_stats(df_sorted, cat_col, 'above_thresh',
                                            y_axis_pct=True, figsize=figsize, figtitle=figtitle,
                                            xticks_map=xticks_map,
                                            figfontsize=figfontsize, figtitlesize=figtitlesize)

        # for continuous variables, plot histograms and calculate some stats
        else:
            self.continuous_plot_and_stats(df_sorted, name_or_prefix, 'above_thresh', nbins,
                                           y_axis_pct=True, figsize=figsize, figtitle=figtitle,
                                           figfontsize=figfontsize, figtitlesize=figtitlesize)
