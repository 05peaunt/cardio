""" contain Batch class for processing ECGs """
import os
import sys
import copy
import itertools
import numpy as np
import pandas as pd

from keras.layers import Convolution1D, MaxPooling1D, Merge,\
GlobalMaxPooling1D, Input, Dense, Dropout, Flatten
from keras.models import Model, model_from_yaml
from keras.optimizers import Adam
import keras.backend as K

from scipy.signal import resample_poly
from sklearn.metrics import f1_score, log_loss
from numba import njit
import wfdb
sys.path.append('..')
import dataset as ds
import wfdb
sys.path.append('..')
import dataset as ds


@njit(nogil=True)
def get_pos_of_max(pred):
    '''
    Returns position of maximal element in a row.
    Arguments
    pred: 2d array.
    '''
    labels = np.zeros(pred.shape)
    for i in range(len(labels)):
        labels[i, pred[i].argmax()] = 1
    return labels


def resample_signal(signal, annot, meta, index, new_fs):
    """
    Resample signal along axis=1 to new sampling rate. Retruns resampled signal with modified meta.
    Resampling of annotation will be implemented in the future.
    Arguments
    signal, annot, meta, index: componets of ecg signal.
    new_fs: target signal sampling rate in Hz.
    """
    fs = meta['fs']
    new_len = int(new_fs * len(signal[0]) / fs)
    signal = resample_poly(signal, new_len, len(signal[0]), axis=1)
    out_meta = {**meta, 'fs': new_fs}
    return [signal, annot, out_meta, index]


def segment_signal(signal, annot, meta, index, length, step, pad, return_copy):
    """
    Segment signal along axis=1 with constant step to segments with constant length.
    If signal is shorter than target segment length, signal is zero-padded on the left if
    pad is True or raise ValueError if pad is False.
    Segmentation of annotation will be implemented in the future.
    Arguments
    signal, annot, meta, index: componets of ecg signal.
    length: length of segment.
    step: step along axis=1.
    pad: whether to apply zero-padding to short signals.
    return_copy: if True, a copy of segments is returned and segments become intependent. If False,
                 segments are not independent, but segmentation runtime becomes almost indepentent on
                 signal length.
    Attention: segmentation of meta and annotation is not implemented yet.
    """
    if signal.ndim != 2:
        raise ValueError('Signal should have ndim = 2, found ndim = {0}'.
                         format(signal.ndim))

    if signal.shape[1] < length:
        if pad:
            pad_len = length - signal.shape[1]
            segments = np.lib.pad(
                signal, ((0, 0), (pad_len, 0)),
                'constant',
                constant_values=(0, 0))[np.newaxis, :, :]
            return [segments, {}, meta, index]
        else:
            raise ValueError('Signal is shorter than segment length: %i < %i' %
                             (signal.shape[1], length))

    shape = signal.shape[:-1] + (signal.shape[-1] - length + 1, length)
    strides = signal.strides + (signal.strides[-1], )
    segments = np.lib.stride_tricks.as_strided(
        signal, shape=shape, strides=strides)[:, ::step, :]
    segments = np.transpose(segments, (1, 0, 2))

    _ = annot
    if return_copy:
        return [
            segments.copy(), wfdb.Annotation(
                recordname=index,
                annotator='atr',
                annsamp=[],
                anntype=[],
                aux=[]), meta, index
        ]
    else:
        return [
            segments, wfdb.Annotation(
                recordname=index,
                annotator='atr',
                annsamp=[],
                anntype=[],
                aux=[]), meta, index
        ]


def drop_noise(signal, annot, meta, index):
    '''
    Drop signals labeled as noise in meta. Retruns input if signal is not labeles as noise and
    retruns None otherwise.
    Arguments
    signal, annot, meta, index: componets of ecg signal.
    '''
    if meta['diag'] == '~':
        return None
    else:
        return [signal, annot, meta, index]


def replace_labels_in_meta(signal, annot, meta, index, new_labels):
    '''
    Replaces diag label by new label.
    Arguments
    new_labels: dict of previous and corresponding new labels.
    '''
    meta.update({'diag': new_labels[meta['diag']]})
    return [signal, annot, meta, index]


def augment_fs_signal(signal, annot, meta, index, distr, params):
    '''
    Augmentation of signal to random sampling rate. New sampling rate is sampled
    from given probability distribution with specified parameters.
    Arguments
    signal, annot, meta, index: componets of ecg signal.
    distr: distribution type, either a name of any distribution from np.random, or
           callable, or 'none', or 'delta'.
    params: dict of parameters and values for distr. ignored if distr='none'.
    '''
    if hasattr(np.random, distr):
        distr_fn = getattr(np.random, distr)
        new_fs = distr_fn(**params)
    elif callable(distr):
        new_fs = distr_fn(**params)
    elif distr == 'none':
        return [signal, annot, meta, index]
    elif distr == 'delta':
        new_fs = params['loc']
    return resample_signal(signal, annot, meta, index, new_fs)


def augment_fs_signal_mult(signal, annot, meta, index, list_of_distr):
    '''
    Multiple augmentation of signal to random sampling rates. New sampling rates are sampled
    from list of probability distributions with specified parameters.
    Arguments
    signal, annot, meta, index: componets of ecg signal.
    list_of_distr: list of tuples (distr, params). See augment_fs_signal for details.
    '''
    res = [
        augment_fs_signal(signal, annot, meta, index, distr_type, params)
        for (distr_type, params) in list_of_distr
    ]
    out_sig = [x[0] for x in res]
    out_annot = [x[1] for x in res]
    out_meta = [x[2] for x in res]
    out_sig.append([])
    return [np.array(out_sig)[:-1], out_annot, out_meta, index]


def selu(x):
    """Scaled Exponential Linear Unit. (Klambauer et al., 2017)
    """
    alpha = 1.6732632423543772848170429916717
    scale = 1.0507009873554804934193349852946
    return scale * K.elu(x, alpha)


class Error(Exception):
    """Base class for custom errors
    """
    pass


class InputDataError(Error):
    """Class for errors that raised at input data 
    evaluation stage.

    """
    pass


class ProcessedDataError(Error):
    """Class for errors that raised after processing
    data. 

    """
    pass


class TestError(Error):
    """Class for errors to be raised if test for batch class methods 
    are failed.
    """
    pass


class EcgBatch(ds.Batch):
    """Сlass for storing batch of ECG (electrocardiogram)
    signals.
    Derived from base class Batch

    Main attributes:
        1. index: numpy array of signal IDs. Usually string names of files
        2. _data: tuple that contains three data structures with
           relevant ECG information:
           signal - numpy array of signals; initialized as np.array of
           None's same size as index.
           annotation - dict with annotation of the signals; initialized as
           empty dict.
           meta - dict with metadata of the signals (sampling rate, etc.);
           initialized as empty dict.

    Main methods:
        1. __init__(self, index, preloaded=None):
            Basic initialization of patient
            in accordance with Batch.__init__
            given base class Batch. Also initializes
            _data attribute.
        2. load(self, src, fmt='wfdb'):
            Load signals from files, either 'wfdb'
            for .mat files, or 'npz' for npz files.
            returns self
        3. dump(self, dst, fmt='nz')
            Create a dump of the batch
            in the folder defined by dst
            in format defined by fmt.
            returns self

    """

    def __init__(self, index, preloaded=None):

        super().__init__(index, preloaded)
        self.signal = np.ndarray(self.indices.shape, dtype=object)
        self.annotation = dict()
        self.meta = dict()

    @ds.model()
    def model_conformal():  #pylint: disable=too-many-locals
        '''
        Simple conv model to test conformal prediction
        '''
        x = Input((3000, 1))
        conv_1 = Convolution1D(4, 4, activation=selu)(x)
        mp_1 = MaxPooling1D()(conv_1)
        conv_2 = Convolution1D(8, 4, activation=selu)(mp_1)
        mp_2 = MaxPooling1D()(conv_2)
        conv_3 = Convolution1D(16, 4, activation=selu)(mp_2)
        mp_3 = MaxPooling1D()(conv_3)
        conv_4 = Convolution1D(32, 4, activation=selu)(mp_3)
        pool = GlobalMaxPooling1D()(conv_4)
        fc_1 = Dense(8, kernel_initializer='uniform', activation='relu')(pool)
        drop = Dropout(0.2)(fc_1)
        fc_2 = Dense(
            2, kernel_initializer='uniform', activation='softmax')(drop)

        opt = Adam()
        model = Model(inputs=x, outputs=fc_2)
        model.compile(optimizer=opt, loss="categorical_crossentropy")

        hist = {
            'train_loss': [],
            'train_metric': [],
            'val_loss': [],
            'val_metric': []
        }
        diag_classes = ['A', 'NonA']

        return model, hist, diag_classes

    @property
    def components(self):
        return "signal", "annotation", "meta"

    @ds.action
    def load(self, src=None, fmt="wfdb"):
        """Load signals, annotations and metadata from files into EcgBatch.

        Args:
            src - dict with indice-path pairs, not needed if index is created
            using path;
            fmt - format of files with data, either 'wfdb' for .mat/.atr/.hea
            files, or 'npz' for .npz files.

        Example:
            index = FilesIndex(path="/some/path/*.dcm", no_ext=True)
            batch = EcgBatch(index)
            batch.load(fmt='wfdb')

        """

        if fmt == "wfdb":
            self._load_wfdb(src=src)  # pylint: disable=no-value-for-parameter
        elif fmt == "npz":
            self._load_npz(src=src)  # pylint: disable=no-value-for-parameter
        else:
            raise TypeError("Incorrect type of source")

        return self

    @ds.action
    @ds.inbatch_parallel(init='indices', target='threads')
    def _load_wfdb(self, index, src=None):
        pos = self.index.get_pos(index)
        if src:
            path = src[index]
        else:
            path = self.index.get_fullpath(index)

        record = wfdb.rdsamp(os.path.splitext(path)[0])
        sig = record.__dict__.pop('p_signals')
        fields = record.__dict__
        self.signal[pos] = sig.T
        self.meta[pos] = fields

        try:
            annot = wfdb.rdann(path, "atr")
            self.annotation[pos] = annot
        except FileNotFoundError:
            self.annotation[pos] = wfdb.Annotation(
                recordname=index,
                annotator='atr',
                annsamp=[],
                anntype=[],
                aux=[])

    @ds.action
    @ds.inbatch_parallel(init='indices', target='threads')
    def _load_npz(self, index, src=None):
        pos = self.index.get_pos(index)
        if src:
            path = src[index]
        else:
            path = self.index.get_fullpath(index)

        data_npz = np.load(path + '.npz')
        self.signal[pos] = data_npz["signal"]
        self.annotation[pos] = data_npz["annotation"]
        self.meta[pos] = data_npz["meta"].item()

    @ds.action
    def dump(self, dst, fmt="npz"):
        """Save each record with annotations and metadata
        in separate files as 'dst/<index>.<fmt>'

        Args:
            dst - string with path to save data to
            fmt - format of files, only 'npz' is supported now

        Example:
            batch = EcgBatch(ind)
            batch.load(...)
            batch.dump(dst='./dump/')

        """

        if fmt == "npz":
            self._dump_npz(dst=dst)  # pylint: disable=no-value-for-parameter
        else:
            raise NotImplementedError("The format is not supported yet")

        return self

    @ds.action
    @ds.inbatch_parallel(init='indices', target='threads')
    def _dump_npz(self, index, dst):
        signal, ann, meta = self[index]
        np.savez(
            os.path.join(dst, index + ".npz"),
            signal=signal,
            annotation=ann,
            meta=meta)

    def input_check_post(self, all_results, *args, **kwargs):
        if ds.any_action_failed(all_results):
            all_errors = self.get_errors(all_results)
            print(all_errors)
            traceback.print_tb(all_errors[0].__traceback__)
            raise ValueError("Checkup failed: failed to assemble results.")

        all_good = np.all(np.array(all_results, dtype="object")[:, 0])
        if not all_good:
            raise InputDataError(
                'Error with input data in function %s' % all_results[0][1])
        return self

    @ds.action
    @ds.inbatch_parallel(
        init='indices', post='input_check_post', target='threads')
    def check_signal_length(self, index, operator=np.greater_equal, length=0):
        pos = self.index.get_pos(index)
        return operator(self.signal[pos].shape[1],
                        length), sys._getframe().f_code.co_name

    def update(self, data=None, annot=None, meta=None):
        """
        Update content of ecg_batch
        """
        if data is not None:
            self.signal = np.array(data)
        if annot is not None:
            self.annotation = annot
        if meta is not None:
            self.meta = meta
        return self

    def init_parallel(self, *args, **kwargs):
        '''
        Return array of ecg with index
        '''
        _ = args, kwargs
        return [[*self[i], i] for i in self.indices]

    def post_parallel(self, all_results, *args, **kwargs):
        #pylint: disable=too-many-locals
        #pylint: disable=too-many-branches
        '''
        Build ecg_batch from a list of items either [signal, annot, meta] or None.
        All Nones are ignored.
        Signal can be either a single signal or a list of signals.
        If signal is a list of signals, annot and meta can be a single annot and meta
        or a list of annots and metas of the same lentgh as the list of signals. In the
        first case annot and meta are broadcasted to each signal in the list of signals.
        Arguments
        all results: list of items either [signal, annot, meta] or None
        '''
        _ = args, kwargs
        if ds.any_action_failed(all_results):
            all_errors = self.get_errors(all_results)
            print(all_errors)
            traceback.print_tb(all_errors[0].__traceback__)
            raise ValueError("Parallelism failed: failed to assemble results.")

        valid_results = [res for res in all_results if res is not None]

        list_of_arrs = [x[0] for x in valid_results]
        list_of_lens = np.array([len(x[0]) for x in valid_results])
        list_of_annot = np.array([x[1] for x in valid_results]).ravel()
        list_of_meta = np.array([x[2] for x in valid_results]).ravel()
        list_of_origs = np.array([x[3] for x in valid_results]).ravel()

        if max(list_of_lens) <= 1:
            ind = ds.DatasetIndex(index=np.array(list_of_origs))
        else:
            ind = ds.DatasetIndex(index=np.arange(
                sum(list_of_lens), dtype=int))
        out_batch = EcgBatch(ind)
        positions = np.array(
            [out_batch.index.get_pos(indice) for indice in ind.indices])

        if list_of_arrs[0].ndim > 3:
            raise ValueError(
                'Signal is expected to have ndim = 1, 2 or 3, found ndim = {0}'
                .format(list_of_arrs[0].ndim))
        if list_of_arrs[0].ndim in [1, 3]:
            list_of_arrs = list(
                itertools.chain([x for y in list_of_arrs for x in y]))
        list_of_arrs.append([])
        batch_data = np.array(list_of_arrs)[:-1]

        if len(ind.indices) == len(list_of_origs):
            origins = list_of_origs
        else:
            origins = np.repeat(list_of_origs, list_of_lens)

        if len(ind.indices) == len(list_of_meta):
            metas = list_of_meta
        else:
            metas = []
            for i, rep in enumerate(list_of_lens):
                for j in range(rep):  #pylint: disable=unused-variable
                    metas.append(copy.deepcopy(list_of_meta[i]))
            metas = np.array(metas)
        for i in range(len(batch_data)):
            metas[i].update({'origin': origins[i]})
        batch_meta = dict(zip(positions, metas))

        if len(ind.indices) == len(list_of_annot):
            annots = list_of_annot
        else:
            annots = []
            for i, rep in enumerate(list_of_lens):
                for j in range(rep):  #pylint: disable=unused-variable
                    annots.append(copy.deepcopy(list_of_annot[i]))
            annots = np.array(annots)
        batch_annot = dict(zip(positions, annots))

        return out_batch.update(
            data=batch_data, annot=batch_annot, meta=batch_meta)

    @ds.action
    @ds.inbatch_parallel(
        init="init_parallel", post="post_parallel", target='mpc')
    def resample(self, new_fs):
        '''
        Resample all signals in batch along axis=1 to new sampling rate. Retruns resampled batch with modified meta.
        Resampling of annotation will be implemented in the future.
        Arguments
        new_fs: target signal sampling rate in Hz.
        '''
        _ = new_fs
        return resample_signal

    @ds.action
    @ds.inbatch_parallel(
        init="init_parallel", post="post_parallel", target='mpc')
    def augment_fs(self, list_of_distr):
        '''
        Multiple augmentation of signals in batch to random sampling rates. New sampling rates are sampled
        from list of probability distributions with specified parameters.
        Arguments
        list_of_distr: list of tuples (distr, params). See augment_fs_signal for details.
        '''
        _ = list_of_distr
        return augment_fs_signal_mult

    @ds.action
    @ds.inbatch_parallel(
        init="init_parallel", post="post_parallel", target='mpc')
    def split_to_segments(self, length, step, pad, return_copy):
        """
        Split signals along axis=1 to segments with constant length.
        If signal is shorter than target segment length, signal is zero-padded on the left if
        pad is True or raise ValueError if pad is False.
        Segmentation of annotation will be implemented in the future.
        Arguments
        length: length of segment.
        step: step along axis=1 of the signal.
        pad: whether to apply zero-padding to short signals.
        return_copy: if True, a copy of segments is returned and segments become intependent. If False,
                 segments are not independent, but segmentation runtime becomes almost indepentent on
                 signal length.
        """
        _ = length, step, pad, return_copy
        return segment_signal

    @ds.action
    @ds.inbatch_parallel(
        init="init_parallel", post="post_parallel", target='mpc')
    def drop_noise(self):
        '''
        Drop signals labeled as noise from the batch.
        Arguments
        None
        '''
        return drop_noise

    @ds.action
    def load_labels(self, path):
        """
        Load labels from file with signal labels. File should have a csv format
        and contain 2 columns: index of ecg and label.
        Arguments
        path: path to the file with labels
        """
        ref = pd.read_csv(path, header=None)
        ref.columns = ['index', 'diag']
        ref = ref.set_index('index')  #pylint: disable=no-member
        for ecg in self.index.indices:
            pos = self.index.get_pos(ecg)
            self.meta[pos]['diag'] = ref.ix[ecg]['diag']
        return self

    @ds.action()
    def replace_labels(self, model_name, new_labels):
        '''
        Replace original labels by new labels.
        Arguments
        model_name: name of the model where to replece labels.
        new_labels: new labels to replace previous.
        '''
        model_comp = list(self.get_model_by_name(model_name))
        model_comp[2] = list(new_labels.values())
        return self.replace_all_labels(new_labels)

    @ds.action()
    @ds.inbatch_parallel(
        init="init_parallel", post="post_parallel", target='mpc')
    def replace_all_labels(self, new_labels):
        '''
        Replace original labels by new labels.
        Arguments
        new_labels: dict of previous and corresponding new labels.
        '''
        _ = new_labels
        return replace_labels_in_meta

    @ds.action()
    def get_categorical_labels(self, model_name):
        '''
        Returns a dummy matrix given an array of categorical variables and list of categories.
        Original labels will be replaced by new labels if encode is not None.
        Arguments
        labels: array of categorical variables
        classes: all possible classes
        '''
        classes = self.get_model_by_name(model_name)[2]
        labels = [
            self.meta[self.index.get_pos(ind)]['diag'] for ind in self.indices
        ]
        return pd.get_dummies(classes + labels).as_matrix()[len(classes):]

    @ds.action()
    def train_on_batch(self, model_name):
        '''
        Train model
        '''
        model_comp = self.get_model_by_name(model_name)
        model, hist, _ = model_comp
        train_x = np.array([x for x in self.signal]).reshape((-1, 3000, 1))
        train_y = self.get_categorical_labels(model_name)
        res = model.train_on_batch(train_x, train_y)
        pred = model.predict(train_x)
        y_pred = get_pos_of_max(pred)
        hist['train_loss'].append(res)
        hist['train_metric'].append(f1_score(train_y, y_pred, average='macro'))
        return self

    @ds.action()
    def validate_on_batch(self, model_name):
        '''
        Validate model
        '''
        model_comp = self.get_model_by_name(model_name)
        model, hist, _ = model_comp
        test_x = np.array([x for x in self.signal]).reshape((-1, 3000, 1))
        test_y = self.get_categorical_labels(model_name)
        pred = model.predict(test_x)
        y_pred = get_pos_of_max(pred)
        hist['val_loss'].append(log_loss(test_y, pred))
        hist['val_metric'].append(f1_score(test_y, y_pred, average='macro'))
        return self

    @ds.action()
    def model_summary(self, model_name):
        '''
        Print model layers
        '''
        model_comp = self.get_model_by_name(model_name)
        print(model_name)
        print(model_comp[0].summary())
        return self

    @ds.action()
    def save_model(self, model_name, fname):
        '''
        Save model layers and weights
        '''
        model_comp = self.get_model_by_name(model_name)
        model = model_comp[0]
        model.save_weights(fname)
        yaml_string = model.to_yaml()
        fout = open(fname + ".layers", "w")
        fout.write(yaml_string)
        fout.close()
        return self

    @ds.action()
    def load_model(self, model_name, fname):
        '''
        Load model layers and weights
        '''
        model_comp = self.get_model_by_name(model_name)
        model = model_comp[0]
        fin = open(fname + ".layers", "r")
        yaml_string = fin.read()
        fin.close()
        model = model_from_yaml(yaml_string)
        model.load_weights(fname)
        return self