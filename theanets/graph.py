# -*- coding: utf-8 -*-

r'''
'''

import climate
import gzip
import hashlib
import pickle
import theano
import theano.tensor as TT

from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

from . import layers

logging = climate.get_logger(__name__)

FLOAT = theano.config.floatX


def load(filename, **kwargs):
    '''Load an entire network from a pickle file on disk.

    If this function is called without extra keyword arguments, a new network
    will be created using the keyword arguments that were originally used to
    create the pickled network. If this helper function is called with extra
    keyword arguments, they will override arguments that were originally used to
    create the pickled network. This override allows one to, for example, load a
    network that was created with one activation function, and apply a different
    activation function to the existing weights. Some options will cause errors
    if overridden, such as `layers` or `tied_weights`, since they change the
    number of parameters in the model.

    Parameters
    ----------
    filename : str
        Load the keyword arguments and parameters of a network from a pickle
        file at the named path. If this name ends in ".gz" then the input will
        automatically be gunzipped; otherwise the input will be treated as a
        "raw" pickle.

    Returns
    -------
    network : :class:`Network`
        A newly-constructed network, with topology and parameters loaded from
        the given pickle file.
    '''
    opener = gzip.open if filename.lower().endswith('.gz') else open
    handle = opener(filename, 'rb')
    pkl = pickle.load(handle)
    handle.close()
    kw = pkl['kwargs']
    kw.update(kwargs)
    net = pkl['klass'](**kw)
    net.load_params(filename)
    return net


class Network(object):
    '''The network class encapsulates a network computation graph.

    In addition to defining standard functionality for common types of
    feedforward nets, there are also many options for specifying topology and
    regularization, several of which must be provided to the constructor at
    initialization time.

    Parameters
    ----------
    layers : sequence of int, tuple, dict, or :class:`Layer <layers.Layer>`
        A sequence of values specifying the layer configuration for the network.
        For more information, please see :ref:`creating-specifying-layers`.
    hidden_activation : str, optional
        The name of an activation function to use on hidden network layers by
        default. Defaults to 'logistic'.
    output_activation : str, optional
        The name of an activation function to use on the output layer by
        default. Defaults to 'linear'.
    rng : theano RandomStreams object, optional
        Use a specific Theano random number generator. A new one will be created
        if this is None.
    weighted : bool, optional
        If True, the network will require an additional input that provides
        weights for the target outputs of the network; the weights will be the
        last input argument to the network, and they must be the same shape as
        the target output. This can be particularly useful for recurrent
        networks, where the length of each input sequence in a minibatch is not
        necessarily the same number of time steps, or for classifier networks
        where the prior proabibility of one class is significantly different
        than another. The default is not to use weighted outputs.

    Attributes
    ----------
    layers : list of :class:`Layer <layers.Layer>`
        A list of the layers in this network model.
    kwargs : dict
        A dictionary containing the keyword arguments used to construct the
        network.
    '''

    def __init__(self, **kwargs):
        self._graphs = {}     # cache of symbolic computation graphs
        self._functions = {}  # cache of callable feedforward functions
        self.layers = []
        self.kwargs = kwargs
        self.inputs = list(self.setup_vars())
        self.setup_layers()

    def setup_vars(self):
        '''Setup Theano variables required by our network.

        The default variable for a network is simply `x`, which represents the
        input to the network.

        Subclasses may override this method to specify additional variables. For
        example, a supervised model might specify an additional variable that
        represents the target output for a particular input.

        Returns
        -------
        vars : list of theano variables
            A list of the variables that this network requires as inputs.
        '''
        # x represents our network's input.
        self.x = TT.matrix('x')

        # the weight array is provided to ensure that different target values
        # are taken into account with different weights during optimization.
        self.weights = TT.matrix('weights')

        if self.kwargs.get('weighted'):
            return [self.x, self.weights]
        return [self.x]

    def error(self, output):
        '''Build a theano expression for computing the network error.

        Parameters
        ----------
        output : theano expression
            A theano expression representing the output of the network.

        Returns
        -------
        error : theano expression
            A theano expression representing the network error.
        '''
        err = output - self.x
        if self.is_weighted:
            return (self.weights * err * err).sum() / self.weights.sum()
        return (err * err).mean()

    def setup_layers(self):
        '''Set up a computation graph for our network.

        The default implementation constructs a series of feedforward
        layers---called the "encoder" layers---and then calls
        :func:`setup_decoder` to construct the decoding apparatus in the
        network.

        Subclasses may override this method to construct alternative network
        topologies.
        '''
        if 'layers' not in self.kwargs:
            return

        specs = list(self.encoding_layers)
        rng = self.kwargs.get('rng') or RandomStreams()

        # setup input layer.
        self.layers.append(
            layers.build('input', specs.pop(0), rng=rng, name='in'))

        # setup "encoder" layers.
        for i, spec in enumerate(specs):
            # if spec is a Layer instance, just add it and move on.
            if isinstance(spec, layers.Layer):
                self.layers.append(spec)
                continue

            # here we set up some defaults for constructing a new layer.
            form = 'feedforward'
            kwargs = dict(
                name='hid{}'.format(len(self.layers)),
                inputs={'{}.out'.format(self.layers[-1].name): self.layers[-1].outputs['out']},
                activation=self.kwargs.get('hidden_activation', 'logistic'),
                rng=rng,
            )

            # by default, spec is assumed to be a lowly integer, giving the
            # number of units in the layer.
            if isinstance(spec, int):
                kwargs['size'] = spec

            # if spec is a tuple, assume that it contains one or more of the following:
            # - the type of layer to construct (layers.Layer subclass)
            # - the name of a class for the layer (str; if layes.Layer subclass)
            # - the name of an activation function (str; otherwise)
            # - the number of units in the layer (int)
            if isinstance(spec, (tuple, list)):
                for el in spec:
                    try:
                        if issubclass(el, layers.Layer):
                            form = el.__name__
                    except TypeError:
                        pass
                    if isinstance(el, str):
                        if el.lower() in layers.Layer._registry:
                            form = el
                        else:
                            kwargs['activation'] = el
                    if isinstance(el, int):
                        kwargs['size'] = el
                kwargs['name'] = '{}{}'.format(form, len(self.layers))

            # if spec is a dictionary, try to extract a form for the layer, and
            # override our default keyword arguments with the rest.
            if isinstance(spec, dict):
                if 'form' in spec:
                    form = spec['form'].lower()
                    kwargs['name'] = '{}{}'.format(form, len(self.layers))
                kwargs.update(spec)

            if isinstance(form, str) and form.lower() == 'bidirectional':
                kwargs['name'] = 'bd{}{}'.format(
                    kwargs.get('worker', 'rnn'), len(self.layers))

            self.layers.append(layers.build(form, **kwargs))

        # setup output layer.
        self.setup_decoder()

    def setup_decoder(self):
        '''Set up the "decoding" computations from layer activations to output.

        The default decoder constructs a single weight matrix for each of the
        hidden layers in the network that should be used for decoding (see the
        `decode_from` parameter) and outputs the sum of the decoders.

        This method can be overridden by subclasses to implement alternative
        decoding strategies.

        Parameters
        ----------
        decode_from : int, optional
            Compute the activation of the output vector using the activations of
            the last N hidden layers in the network. Defaults to 1, which
            results in a traditional setup that decodes only from the
            penultimate layer in the network.
        '''
        sizes = [l.size for l in self.layers]
        back = self.kwargs.get('decode_from', 1)
        self.layers.append(layers.build(
            'feedforward',
            name='out',
            nin=sizes[-1] if back <= 1 else sizes[-back:],
            size=self.kwargs['layers'][-1],
            activation=self.output_activation))

    @property
    def is_weighted(self):
        '''True iff the network uses explicit target weights.'''
        return bool(self.kwargs.get('weighted'))

    @property
    def output_activation(self):
        '''A string describing the output activation for this network.'''
        return self.kwargs.get('output_activation', 'linear')

    @property
    def encoding_layers(self):
        '''List of layers that will be part of the network encoder.

        This property is used by the default implementation of
        :func:`setup_layers` to determine which layers in the network will be
        treated as "encoding" layers. The default is to treat all but the last
        layer as encoders.

        Returns
        -------
        layers : list of int, dict, etc.
            A list of specifications for encoder layers of the network.
        '''
        return self.kwargs['layers'][:-1]

    def _hash(self, **kwargs):
        '''Construct a string key for representing a computation graph.

        This key will be unique for a given network topology and set of keyword
        arguments.

        Returns
        -------
        key : str
            A hash representing the computation graph for the current network.
        '''
        def add(s):
            h.update(str(s).encode('utf-8'))
        h = hashlib.md5()
        add(kwargs)
        for l in self.layers:
            add('{}{}{}'.format(l.__class__.__name__, l.name, l.size))
        return h.hexdigest()

    def build_graph(self, **kwargs):
        '''Connect the layers in this network to form a computation graph.

        Parameters
        ----------
        input_noise : float, optional
            Standard deviation of desired noise to inject into input.
        hidden_noise : float, optional
            Standard deviation of desired noise to inject into hidden unit
            activation output.
        input_dropouts : float in [0, 1], optional
            Proportion of input units to randomly set to 0.
        hidden_dropouts : float in [0, 1], optional
            Proportion of hidden unit activations to randomly set to 0.

        Returns
        -------
        outputs : list of theano variables
            A list of expressions giving the output of each layer in the graph.
        monitors : list of (name, expression) tuples
            A list of expressions to use when monitoring the network.
        updates : list of update tuples
            A list of updates that should be performed by a theano function that
            computes something using this graph.
        '''
        key = self._hash(**kwargs)
        if key not in self._graphs:
            outputs, monitors, updates = [], [], []
            for i, layer in enumerate(self.layers):
                noise = dropout = 0
                if i == 0:
                    # input to first layer is data.
                    inputs = self.x
                    noise = kwargs.get('input_noise', 0)
                    dropout = kwargs.get('input_dropouts', 0)
                elif i == len(self.layers) - 1:
                    # inputs to last layer is output of layers to decode.
                    inputs = outputs[-self.kwargs.get('decode_from', 1):]
                    noise = kwargs.get('hidden_noise', 0)
                    dropout = kwargs.get('hidden_dropouts', 0)
                else:
                    # inputs to other layers are outputs of previous layer.
                    inputs = outputs[-1]
                out, mon, upd = layer.output(inputs, noise=noise, dropout=dropout)
                outputs.append(out)
                monitors.extend(mon)
                updates.extend(upd)
            self._graphs[key] = outputs, monitors, updates
        return self._graphs[key]

    @property
    def params(self):
        '''Get a list of the learnable theano parameters for this network.

        This attribute is mostly used by :class:`Trainer
        <theanets.trainer.Trainer>` implementations to compute the set of
        parameters that are tunable in a network.

        Returns
        -------
        params : list of theano variables
            A list of parameters that can be learned in this model.
        '''
        return [p for l in self.layers for p in l.params]

    @property
    def num_params(self):
        '''Number of parameters in the entire network model.'''
        return sum(l.num_params for l in self.layers)

    def find(self, layer, param):
        '''Get a parameter from a layer in the network.

        Parameters
        ----------
        layer : int or str
            The layer that owns the parameter to return.

            If this is an integer, then 0 refers to the input layer, 1 refers
            to the first hidden layer, 2 to the second, and so on.

            If this is a string, the layer with the corresponding name, if any,
            will be used.

        param : int or str
            Name of the parameter to retrieve from the specified layer, or its
            index in the parameter list of the layer.

        Raises
        ------
        KeyError
            If there is no such layer, or if there is no such parameter in the
            specified layer.

        Returns
        -------
        param : theano shared variable
            A shared parameter variable from the indicated layer.
        '''
        for i, l in enumerate(self.layers):
            if layer == i or layer == l.name:
                return l.find(param)
        raise KeyError(layer)

    def feed_forward(self, x, **kwargs):
        '''Compute a forward pass of all layers from the given input.

        All keyword arguments are passed directly to :func:`build_graph`.

        Parameters
        ----------
        x : ndarray (num-examples, num-variables)
            An array containing data to be fed into the network. Multiple
            examples are arranged as rows in this array, with columns containing
            the variables for each example.

        Returns
        -------
        layers : list of ndarray (num-examples, num-units)
            The activation values of each layer in the the network when given
            input `x`. For each of the hidden layers, an array is returned
            containing one row per input example; the columns of each array
            correspond to units in the respective layer. The "output" of the
            network is the last element of this list.
        '''
        key = self._hash(**kwargs)
        if key not in self._functions:
            outputs, _, updates = self.build_graph(**kwargs)
            self._functions[key] = theano.function(
                [self.x], outputs, updates=updates)
        return self._functions[key](x)

    def predict(self, x):
        '''Compute a forward pass of the inputs, returning the network output.

        Parameters
        ----------
        x : ndarray (num-examples, num-variables)
            An array containing data to be fed into the network. Multiple
            examples are arranged as rows in this array, with columns containing
            the variables for each example.

        Returns
        -------
        y : ndarray (num-examples, num-variables
            Returns the values of the network output units when given input `x`.
            Rows in this array correspond to examples, and columns to output
            variables.
        '''
        return self.feed_forward(x)[-1]

    __call__ = predict

    def save(self, filename):
        '''Save the state of this network to a pickle file on disk.

        Parameters
        ----------
        filename : str
            Save the parameters of this network to a pickle file at the named
            path. If this name ends in ".gz" then the output will automatically
            be gzipped; otherwise the output will be a "raw" pickle.
        '''
        state = dict(klass=self.__class__, kwargs=self.kwargs)
        for layer in self.layers:
            key = '{}-values'.format(layer.name)
            state[key] = [p.get_value() for p in layer.params]
        opener = gzip.open if filename.lower().endswith('.gz') else open
        handle = opener(filename, 'wb')
        pickle.dump(state, handle, -1)
        handle.close()
        logging.info('%s: saved model parameters', filename)

    def load_params(self, filename):
        '''Load the parameters for this network from disk.

        Parameters
        ----------
        filename : str
            Load the parameters of this network from a pickle file at the named
            path. If this name ends in ".gz" then the input will automatically
            be gunzipped; otherwise the input will be treated as a "raw" pickle.
        '''
        opener = gzip.open if filename.lower().endswith('.gz') else open
        handle = opener(filename, 'rb')
        saved = pickle.load(handle)
        handle.close()
        for layer in self.layers:
            for p, v in zip(layer.params, saved['{}-values'.format(layer.name)]):
                p.set_value(v)
        logging.info('%s: loaded model parameters', filename)

    def extra_monitors(self, outputs):
        '''Construct extra monitors for this network.

        Parameters
        ----------
        outputs : list of theano expressions
            A list of theano expressions describing the activations of each
            layer in the network.

        Returns
        -------
        monitors : sequence of (name, expression) tuples
            A sequence of named monitor quantities.
        '''
        return []

    def loss(self, **kwargs):
        '''Return a variable representing the loss for this network.

        The loss includes both the error for the network as well as any
        regularizers that are in place.

        Parameters
        ----------
        weight_l1 : float, optional
            Regularize the L1 norm of unit connection weights by this constant.
        weight_l2 : float, optional
            Regularize the L2 norm of unit connection weights by this constant.
        hidden_l1 : float, optional
            Regularize the L1 norm of hidden unit activations by this constant.
        hidden_l2 : float, optional
            Regularize the L2 norm of hidden unit activations by this constant.
        contractive : float, optional
            Regularize model using the Frobenius norm of the hidden Jacobian.

        Returns
        -------
        loss : theano expression
            A theano expression representing the loss of this network.
        monitors : list of (name, expression) pairs
            A list of named monitor expressions to compute for this network.
        updates : list of (parameter, expression) pairs
            A list of named parameter update expressions for this network.
        '''
        outputs, monitors, updates = self.build_graph(**kwargs)
        err = self.error(outputs[-1])
        monitors.insert(0, ('err', err))
        monitors.extend(self.extra_monitors(outputs))
        hiddens = outputs[1:-1]
        regularizers = dict(
            weight_l1=(abs(w).sum() for l in self.layers for w in l.params),
            weight_l2=((w * w).sum() for l in self.layers for w in l.params),
            hidden_l1=(abs(h).mean(axis=0).sum() for h in hiddens),
            hidden_l2=((h * h).mean(axis=0).sum() for h in hiddens),
            contractive=(TT.sqr(TT.grad(h.mean(axis=0).sum(), self.x)).sum()
                         for h in hiddens),
        )
        regularization = (TT.cast(kwargs[weight], FLOAT) * sum(expr)
                          for weight, expr in regularizers.items()
                          if kwargs.get(weight, 0) > 0)
        return err + sum(regularization), monitors, updates
