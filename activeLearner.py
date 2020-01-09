import abc
import argparse
import pickle
import random
import sys
from collections import defaultdict
from pprint import pprint

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score
from sklearn.utils.class_weight import compute_sample_weight

from experiment_setup_lib import print_data_segmentation, classification_report_and_confusion_matrix


class ActiveLearner:
    def __init__(self, config):
        np.random.seed(config.random_seed)
        random.seed(config.random_seed)

        self.nr_learning_iterations = config.nr_learning_iterations
        self.nr_queries_per_iteration = config.nr_queries_per_iteration

        self.best_hyper_parameters = {
            'random_state': config.random_seed,
            'n_jobs': config.cores
        }

        # it's a list because of committee (in all other cases it's just one classifier)
        self.clf_list = [RandomForestClassifier(**self.best_hyper_parameters)]

        self.query_accuracy_list = []

        self.metrics_per_al_cycle = {
            'test_data_metrics': [[] for clf in self.clf_list],
            'train_labeled_data_metrics': [[] for clf in self.clf_list],
            'train_unlabeled_data_metrics': [[] for clf in self.clf_list],
            'train_unlabeled_class_distribution': [[] for clf in self.clf_list],
            'stop_certainty_list': [],
            'stop_stddev_list': [],
            'stop_accuracy_list': [],
            'query_length': [],
        }

        self.len_queries = self.nr_learning_iterations * self.nr_queries_per_iteration
        self.config = config

    def set_data(self, X_train_labeled, Y_train_labeled, X_train_unlabeled,
                 Y_train_unlabeled, X_test, Y_test, label_encoder):
        self.X_train_labeled = X_train_labeled
        self.Y_train_labeled = Y_train_labeled
        self.X_train_unlabeled = X_train_unlabeled
        self.Y_train_unlabeled = Y_train_unlabeled
        self.X_test = X_test
        self.Y_test = Y_test

        self.label_encoder = label_encoder

        self.classifier_classes = [
            i for i in range(0, len(label_encoder.classes_))
        ]

    def calculate_stopping_criteria_stddev(self):
        accuracy_list = self.query_accuracy_list
        k = 5

        if len(accuracy_list) < k:
            self.metrics_per_al_cycle['stop_stddev_list'].append(float('NaN'))

        k_list = accuracy_list[-k:]
        stddev = np.std(k_list)
        self.metrics_per_al_cycle['stop_stddev_list'].append(stddev)

    def calculate_stopping_criteria_accuracy(self):
        # we use the accuracy ONLY for the current selected query
        self.metrics_per_al_cycle['stop_accuracy_list'].append(
            self.query_accuracy_list[-1])

    def calculate_stopping_criteria_certainty(self):
        Y_train_unlabeled_pred = self.clf_list[0].predict(
            self.X_train_unlabeled)
        Y_train_unlabeled_pred_proba = self.clf_list[0].predict_proba(
            self.X_train_unlabeled)

        # don't ask
        test = pd.Series(Y_train_unlabeled_pred)
        test1 = pd.Series(self.classifier_classes)

        indices = test.map(lambda x: np.where(test1 == x)[0][0]).tolist()

        class_certainties = [
            Y_train_unlabeled_pred_proba[i][indices[i]]
            for i in range(len(Y_train_unlabeled_pred_proba))
        ]

        result = np.min(class_certainties)
        self.metrics_per_al_cycle['stop_certainty_list'].append(result)

    @abc.abstractmethod
    def calculate_next_query_indices(self, *args):
        pass

    def fit_clf(self):
        self.clf_list[0].fit(self.X_train_labeled,
                             self.Y_train_labeled,
                             sample_weight=compute_sample_weight(
                                 'balanced', self.Y_train_labeled))

    def calculate_current_metrics(self, X_query, Y_query):
        # calculate for stopping criteria the accuracy of the prediction for the selected queries
        self.query_accuracy_list.append(accuracy_score(Y_query, self.clf_list[0].predict(X_query)))


        for i, clf in enumerate(self.clf_list):
            metrics = classification_report_and_confusion_matrix(
                clf,
                self.X_test,
                self.Y_test,
                self.config,
                self.label_encoder,
                output_dict=True)

            self.metrics_per_al_cycle['test_data_metrics'][i].append(metrics)

            metrics = classification_report_and_confusion_matrix(
                clf,
                self.X_train_labeled,
                self.Y_train_labeled,
                self.config,
                self.label_encoder,
                output_dict=True)

            self.metrics_per_al_cycle['train_labeled_data_metrics'][i].append(
                metrics)

            metrics = classification_report_and_confusion_matrix(
                clf,
                self.X_train_unlabeled,
                self.Y_train_unlabeled,
                self.config,
                self.label_encoder,
                output_dict=True)

            self.metrics_per_al_cycle['train_unlabeled_data_metrics'][i].append(
                metrics)

            train_unlabeled_class_distribution = defaultdict(int)

            for label in self.label_encoder.inverse_transform(Y_query):
                train_unlabeled_class_distribution[label] += 1

            self.metrics_per_al_cycle['train_unlabeled_class_distribution'][i].append(train_unlabeled_class_distribution)

    def increase_labeled_dataset(self):

        # ask strategy for new datapoint
        query_indices = self.calculate_next_query_indices()

        X_query = self.X_train_unlabeled.iloc()[query_indices]

        # ask oracle for new query
        Y_query = self.Y_train_unlabeled[query_indices]

        # move new queries from unlabeled to labeled dataset
        self.X_train_labeled = self.X_train_labeled.append(X_query, ignore_index=True)
        self.X_train_unlabeled.drop(X_query.index, inplace=True)

        self.Y_train_labeled = np.append(self.Y_train_labeled, Y_query)
        self.Y_train_unlabeled = np.delete(self.Y_train_unlabeled, query_indices, 0)


        return X_query, Y_query

    def learn(self):
        print_data_segmentation(self.X_train_labeled, self.X_train_unlabeled,
                                self.X_test, self.len_queries)

        for i in range(0, self.nr_learning_iterations):
            if self.X_train_unlabeled.shape[0] < self.nr_queries_per_iteration:
                break

            print("Iteration: %d" % i)
            print(self.X_train_unlabeled.shape[0])

            # retrain classifier
            self.fit_clf()

            X_query, Y_query = self.increase_labeled_dataset()
            self.metrics_per_al_cycle['query_length'].append(len(Y_query))

            # calculate new metrics
            self.calculate_current_metrics(X_query, Y_query)

            self.calculate_stopping_criteria_accuracy()
            self.calculate_stopping_criteria_stddev()
            self.calculate_stopping_criteria_certainty()



        # in case we specified more queries than we have data
        self.nr_learning_iterations = i
        self.len_queries = self.nr_learning_iterations * self.nr_queries_per_iteration

        pprint(self.metrics_per_al_cycle)
        with open(
                self.config.output + '/' + self.config.strategy + '_' +
                str(self.config.start_size) + '_' +
                str(self.config.nQueriesPerIteration) + '.pickle', 'wb') as f:
            pickle.dump(self.metrics_per_al_cycle, f, pickle.HIGHEST_PROTOCOL)

        clf_active = self.clf_list[0]

        clf_passive_full = RandomForestClassifier(**self.best_hyper_parameters)
        clf_passive_starter = RandomForestClassifier(
            **self.best_hyper_parameters)

        return self.clf_list, self.metrics_per_al_cycle