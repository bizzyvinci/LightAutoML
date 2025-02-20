"""Base classes for machine learning algorithms."""

from abc import ABC, abstractmethod
from copy import copy
from typing import Optional, Tuple, Any, List, cast, Dict, Sequence, Union

import numpy as np

from lightautoml.validation.base import TrainValidIterator
from ..dataset.base import LAMLDataset
from ..dataset.np_pd_dataset import NumpyDataset, CSRSparseDataset, PandasDataset
from ..dataset.roles import NumericRole
from ..utils.logging import get_logger
from ..utils.timer import TaskTimer, PipelineTimer

logger = get_logger(__name__)
TabularDataset = Union[NumpyDataset, CSRSparseDataset, PandasDataset]


class MLAlgo(ABC):
    """
    Abstract class for machine learning algorithm.
    Assume that features are already selected,
    but parameters my be tuned and set before training.
    """
    _default_params: Dict = {}
    # TODO: add checks here
    _fit_checks: Tuple = ()
    _transform_checks: Tuple = ()
    _params: Dict = None
    _name = 'AbstractAlgo'

    @property
    def name(self) -> str:
        """Get model name."""
        return self._name

    @property
    def features(self) -> List[str]:
        """Get list of features."""
        return self._features

    @features.setter
    def features(self, val: Sequence[str]):
        """List of features."""
        self._features = list(val)

    @property
    def is_fitted(self) -> bool:
        """Get flag is the model fitted or not."""
        return self.features is not None

    @property
    def params(self) -> dict:
        """Get model's params dict."""
        if self._params is None:
            self._params = copy(self.default_params)
        return self._params

    @params.setter
    def params(self, new_params: dict):
        assert isinstance(new_params, dict)
        self._params = {**self.params, **new_params}

    def init_params_on_input(self, train_valid_iterator: TrainValidIterator) -> dict:
        """Init params depending on input data.

        Args:
            train_valid_iterator: Classic cv-iterator.

        Returns:
            Dict with model hyperparameters.

        """
        return self.params

    # TODO: Think about typing
    def __init__(self, default_params: Optional[dict] = None, freeze_defaults: bool = True, timer: Optional[TaskTimer] = None):
        """

        Args:
            default_params: Algo hyperparams.
            freeze_defaults:
                - ``True`` :  params may be rewrited depending on dataset.
                - ``False``:  params may be changed only manually
                  or with tuning.
            timer: Timer for Algo.

        """
        self.task = None

        self.freeze_defaults = freeze_defaults
        if default_params is None:
            default_params = {}

        self.default_params = {**self._default_params, **default_params}

        self.models = []
        self._features = None

        self.timer = timer
        if timer is None:
            self.timer = PipelineTimer().start().get_task_timer()

        self._nan_rate = None

    @abstractmethod
    def fit_predict(self, train_valid_iterator: TrainValidIterator) -> LAMLDataset:
        """Abstract method.

        Fit new algo on iterated datasets and predict on valid parts.

        Args:
            train_valid_iterator: Classic cv-iterator.

        """
        # self._features = train_valid_iterator.features

    @abstractmethod
    def predict(self, test: LAMLDataset) -> LAMLDataset:
        """Predict target for input data.

        Args:
            test: Dataset on test.

        Returns:
            Dataset with predicted values.

        """

    def score(self, dataset: LAMLDataset) -> float:
        """Score prediction on dataset with defined metric.

        Args:
            dataset: Dataset with ground truth and predictions.

        Returns:
            Metric value.

        """
        assert self.task is not None, 'No metric defined. Should be fitted on dataset first.'
        metric = self.task.get_dataset_metric()

        return metric(dataset, dropna=True)

    def set_prefix(self, prefix: str):
        """Set prefix to separate models from different levels/pipelines.

        Args:
            prefix: String with prefix.
            
        """
        self._name = '_'.join([prefix, self._name])

    def set_timer(self, timer: TaskTimer) -> 'MLAlgo':
        """Set timer."""
        self.timer = timer

        return self


class TabularMLAlgo(MLAlgo):
    """Machine learning algorithms that accepts numpy arrays as input."""
    _name: str = 'TabularAlgo'

    def _set_prediction(self, dataset: NumpyDataset, preds_arr: np.ndarray) -> NumpyDataset:
        """Insert predictions to dataset with. Inplace transformation.

        Args:
            dataset: Dataset to transform.
            preds_arr: Array with predicted values.

        Returns:
            Transformed dataset.

        """

        prefix = '{0}_prediction'.format(self._name)
        prob = self.task.name in ['binary', 'multiclass']
        dataset.set_data(preds_arr, prefix, NumericRole(np.float32, force_input=True, prob=prob))

        return dataset

    def fit_predict_single_fold(self, train: TabularDataset, valid: TabularDataset) -> Tuple[Any, np.ndarray]:
        """Train on train dataset and predict on holdout dataset.

        Args:
            train: Train Dataset.
            valid: Validation Dataset.

        Returns:
            Target predictions for valid dataset.

        """
        raise NotImplementedError

    def fit_predict(self, train_valid_iterator: TrainValidIterator) -> NumpyDataset:
        """Fit and then predict accordig the strategy that uses train_valid_iterator.

        If item uses more then one time it will
        predict mean value of predictions.
        If the element is not used in training then
        the prediction will be ``numpy.nan`` for this item

        Args:
            train_valid_iterator: Classic cv-iterator.

        Returns:
            Dataset with predicted values.

        """
        logger.info('Start fitting {} ...'.format(self._name))
        self.timer.start()

        assert self.is_fitted is False, 'Algo is already fitted'
        # init params on input if no params was set before
        if self._params is None:
            self.params = self.init_params_on_input(train_valid_iterator)

        # save features names
        self._features = train_valid_iterator.features
        # get metric and loss if None
        self.task = train_valid_iterator.train.task

        # get empty validation data to write prediction
        # TODO: Think about this cast
        preds_ds = cast(NumpyDataset, train_valid_iterator.get_validation_data().empty().to_numpy())

        outp_dim = 1
        if self.task.name == 'multiclass':
            outp_dim = int(np.max(preds_ds.target) + 1)
        # save n_classes to infer params
        self.n_classes = outp_dim

        preds_arr = np.zeros((preds_ds.shape[0], outp_dim), dtype=np.float32)
        counter_arr = np.zeros((preds_ds.shape[0], 1), dtype=np.float32)

        # TODO: Make parallel version later
        for n, (idx, train, valid) in enumerate(train_valid_iterator):
            logger.info('\n===== Start working with fold {} for {} =====\n'.format(n, self._name))
            self.timer.set_control_point()

            model, pred = self.fit_predict_single_fold(train, valid)
            self.models.append(model)
            preds_arr[idx] += pred.reshape((pred.shape[0], -1))
            counter_arr[idx] += 1

            self.timer.write_run_info()

            if (n + 1) != len(train_valid_iterator):
                # split into separate cases because timeout checking affects parent pipeline timer
                if self.timer.time_limit_exceeded():
                    logger.warning('Time limit exceeded after calculating fold {0}'.format(n))
                    break

        logger.debug('Time history {0}. Time left {1}'.format(self.timer.get_run_results(), self.timer.time_left))

        preds_arr /= np.where(counter_arr == 0, 1, counter_arr)
        preds_arr = np.where(counter_arr == 0, np.nan, preds_arr)

        preds_ds = self._set_prediction(preds_ds, preds_arr)
        logger.info('{} fitting and predicting completed'.format(self._name))
        return preds_ds

    def predict_single_fold(self, model: Any, dataset: TabularDataset) -> np.ndarray:
        """Implements prediction on single fold.

        Args:
            model: Model uses to predict.
            dataset: Dataset used for prediction.

        Returns:
            Predictions for input dataset.

        """
        raise NotImplementedError

    def predict(self, dataset: TabularDataset) -> NumpyDataset:
        """Mean prediction for all fitted models.

        Args:
            dataset: Dataset used for prediction.

        Returns:
            Dataset with predicted values.

        """
        assert self.models != [], 'Should be fitted first.'
        preds_ds = dataset.empty().to_numpy()
        preds_arr = None

        for model in self.models:
            if preds_arr is None:
                preds_arr = self.predict_single_fold(model, dataset)
            else:
                preds_arr += self.predict_single_fold(model, dataset)

        preds_arr /= len(self.models)
        preds_arr = preds_arr.reshape((preds_arr.shape[0], -1))
        preds_ds = self._set_prediction(preds_ds, preds_arr)

        return preds_ds
