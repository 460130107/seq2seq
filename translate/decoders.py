import tensorflow as tf
import functools
import math
from tensorflow.python.ops import rnn_cell, rnn
from translate.rnn import get_variable_unsafe, linear_unsafe, multi_rnn_unsafe
from translate.rnn import multi_bidirectional_rnn_unsafe, unsafe_decorator, MultiRNNCell, GRUCell
from collections import namedtuple


def multi_encoder(encoder_inputs, encoders, encoder_input_length, dropout=None, **kwargs):
    """
    Build multiple encoders according to the configuration in `encoders`, reading from `encoder_inputs`.
    The result is a list of the outputs produced by those encoders (for each time-step), and their final state.

    :param encoder_inputs: list of tensors of shape (batch_size, input_length) (one tensor for each encoder)
    :param encoders: list of encoder configurations
    :param encoder_input_length: list of tensors of shape (batch_size) (one tensor for each encoder)
    :param dropout: scalar tensor or None, specifying the keep probability (1 - dropout)
    :return:
      encoder outputs: a list of tensors of shape (batch_size, input_length, encoder_cell_size)
      encoder state: concatenation of the final states of all encoders, tensor of shape (batch_size, sum_of_state_sizes)
    """
    assert len(encoder_inputs) == len(encoders)
    encoder_states = []
    encoder_outputs = []

    # create embeddings in the global scope (allows sharing between encoder and decoder)
    embedding_variables = []
    for encoder in encoders:
        # inputs are token ids, which need to be mapped to vectors (embeddings)
        if not encoder.binary:
            if encoder.get('embedding') is not None:
                initializer = encoder.embedding
                embedding_shape = None
            else:
                # initializer = tf.random_uniform_initializer(-math.sqrt(3), math.sqrt(3))
                initializer = None
                embedding_shape = [encoder.vocab_size, encoder.embedding_size]

            with tf.device('/cpu:0'):
                embedding = get_variable_unsafe('embedding_{}'.format(encoder.name), shape=embedding_shape,
                                                initializer=initializer)
            embedding_variables.append(embedding)
        else:  # do nothing: inputs are already vectors
            embedding_variables.append(None)

    for i, encoder in enumerate(encoders):
        with tf.variable_scope('encoder_{}'.format(encoder.name)):
            encoder_inputs_ = encoder_inputs[i]
            encoder_input_length_ = encoder_input_length[i]

            # TODO: use state_is_tuple=True
            if encoder.use_lstm:
                cell = rnn_cell.BasicLSTMCell(encoder.cell_size, state_is_tuple=False)
                # cell = rnn_cell.LSTMCell(encoder.cell_size, state_is_tuple=False)
            else:
                cell = GRUCell(encoder.cell_size)

            if dropout is not None:
                cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=dropout)

            embedding = embedding_variables[i]

            if embedding is not None or encoder.input_layers:
                batch_size = tf.shape(encoder_inputs_)[0]  # TODO: fix this time major stuff
                time_steps = tf.shape(encoder_inputs_)[1]

                if embedding is None:
                    size = encoder_inputs_.get_shape()[2].value
                    flat_inputs = tf.reshape(encoder_inputs_, [tf.mul(batch_size, time_steps), size])
                else:
                    flat_inputs = tf.reshape(encoder_inputs_, [tf.mul(batch_size, time_steps)])
                    flat_inputs = tf.nn.embedding_lookup(embedding, flat_inputs)

                if encoder.input_layers:
                    for j, size in enumerate(encoder.input_layers):
                        name = 'input_layer_{}'.format(j)
                        flat_inputs = tf.nn.tanh(linear_unsafe(flat_inputs, size, bias=True, scope=name))
                        if dropout is not None:
                            flat_inputs = tf.nn.dropout(flat_inputs, dropout)

                encoder_inputs_ = tf.reshape(flat_inputs,
                                             tf.pack([batch_size, time_steps, flat_inputs.get_shape()[1].value]))

            sequence_length = encoder_input_length_
            parameters = dict(
                inputs=encoder_inputs_, sequence_length=sequence_length, time_pooling=encoder.time_pooling,
                pooling_avg=encoder.pooling_avg, dtype=tf.float32, swap_memory=encoder.swap_memory,
                parallel_iterations=encoder.parallel_iterations, residual_connections=encoder.residual_connections,
                trainable_initial_state=True
            )

            if encoder.bidir:
                encoder_outputs_, _, _ = multi_bidirectional_rnn_unsafe(
                    cells=[(cell, cell)] * encoder.layers, **parameters)
                # Like Bahdanau et al., we use the first annotation h_1 of the backward encoder
                encoder_state_ = encoder_outputs_[:, 0, encoder.cell_size:]
                # TODO: if multiple layers combine last states with a Maxout layer
            else:
                encoder_outputs_, encoder_state_ = multi_rnn_unsafe(
                    cells=[cell] * encoder.layers, **parameters)
                encoder_state_ = encoder_outputs_[:, -1, :]

            encoder_outputs.append(encoder_outputs_)
            encoder_states.append(encoder_state_)

        encoder_state = tf.concat(1, encoder_states)
        return encoder_outputs, encoder_state


def mixer_encoder(encoder_inputs, encoders, encoder_input_length, dropout=None, window_size=5, max_input_len=200,
                  **kwargs):
    assert len(encoder_inputs) == len(encoders)
    assert window_size % 2 == 1
    half_window = window_size // 2
    encoder_outputs = []

    # create embeddings in the global scope (allows sharing between encoder and decoder)
    for i, encoder in enumerate(encoders):
        # inputs are token ids, which need to be mapped to vectors (embeddings)
        # initializer = tf.random_uniform_initializer(-math.sqrt(3), math.sqrt(3))
        initializer = None
        embedding_shape = [encoder.vocab_size, encoder.embedding_size]

        with tf.device('/cpu:0'):
            embedding = get_variable_unsafe('embedding_{}'.format(encoder.name), shape=embedding_shape,
                                            initializer=initializer)
            pos_embedding = get_variable_unsafe('pos_embedding_{}'.format(encoder.name),
                                                shape=[max_input_len, encoder.embedding_size],
                                                initializer=initializer)

        with tf.variable_scope('mixer_encoder'):
            # TODO: MIXER uses a constant (non-trainable) array of ones here, see which one is better
            filter_shape = [window_size, 1, 1, 1]
            filter_ = get_variable_unsafe('filter_{}'.format(encoder.name), filter_shape)

        encoder_inputs_ = encoder_inputs[i]
        # input_length_ = encoder_input_length[i]

        batch_size = tf.shape(encoder_inputs_)[0]
        time_steps = tf.shape(encoder_inputs_)[1]
        # positions start at `1`, `0` is reserved for dummy words
        positions = tf.range(1, time_steps + 1)
        positions = tf.tile(positions, [batch_size])
        positions = tf.reshape(positions, tf.pack([batch_size, time_steps]))

        # this only works because _PAD symbol's index in the vocabulary is 0
        # for other values substract before padding, then add after padding
        # TODO: maybe use a different symbol than _PAD here, this has a different semantic
        encoder_inputs_ = tf.pad(encoder_inputs_, [[0, 0], [half_window, half_window]])
        # `0` position for dummy words
        # TODO: use mask to put 0 for each _PAD symbol
        positions = tf.pad(positions, [[0, 0], [half_window, half_window]])

        inputs_ = tf.nn.embedding_lookup(embedding, encoder_inputs_)   # batch_size * time_steps * embedding_size
        positions = tf.nn.embedding_lookup(pos_embedding, positions)
        inputs_ = inputs_ + positions

        inputs_ = tf.expand_dims(inputs_, 3)  # add 1 dimension (`in_channels`) for conv2d

        outputs_ = tf.nn.conv2d(inputs_, filter_, [1, 1, 1, 1], 'VALID') / window_size
        outputs_ = tf.squeeze(outputs_, [3])
        encoder_outputs.append(outputs_)

    return encoder_outputs, None


def compute_energy(hidden, state, attn_size, **kwargs):
    input_size = hidden.get_shape()[3].value
    batch_size = tf.shape(hidden)[0]
    time_steps = tf.shape(hidden)[1]

    initializer = tf.random_normal_initializer(stddev=0.001)
    y = linear_unsafe(state, attn_size, True, scope='W_a', initializer=initializer)
    y = tf.reshape(y, [-1, 1, attn_size])

    k = get_variable_unsafe('U_a', [input_size, attn_size], initializer=initializer)

    # dot product between tensors requires reshaping
    hidden = tf.reshape(hidden, tf.pack([tf.mul(batch_size, time_steps), input_size]))
    f = tf.matmul(hidden, k)
    f = tf.reshape(f, tf.pack([batch_size, time_steps, attn_size]))

    v = get_variable_unsafe('v_a', [attn_size])
    s = f + y

    return tf.reduce_sum(v * tf.tanh(s), [2])


def compute_energy_with_filter(hidden, state, prev_weights, attention_filters, attention_filter_length,
                               **kwargs):
    time_steps = tf.shape(hidden)[1]
    attn_size = hidden.get_shape()[3].value
    batch_size = tf.shape(hidden)[0]

    filter_shape = [attention_filter_length * 2 + 1, 1, 1, attention_filters]
    filter_ = get_variable_unsafe('filter', filter_shape)
    u = get_variable_unsafe('U', [attention_filters, attn_size])
    prev_weights = tf.reshape(prev_weights, tf.pack([batch_size, time_steps, 1, 1]))
    conv = tf.nn.conv2d(prev_weights, filter_, [1, 1, 1, 1], 'SAME')
    shape = tf.pack([tf.mul(batch_size, time_steps), attention_filters])
    conv = tf.reshape(conv, shape)
    z = tf.matmul(conv, u)
    z = tf.reshape(z, tf.pack([batch_size, time_steps, 1, attn_size]))

    y = linear_unsafe(state, attn_size, True)
    y = tf.reshape(y, [-1, 1, 1, attn_size])

    k = get_variable_unsafe('W', [attn_size, attn_size])

    # dot product between tensors requires reshaping
    hidden = tf.reshape(hidden, tf.pack([tf.mul(batch_size, time_steps), attn_size]))
    f = tf.matmul(hidden, k)
    f = tf.reshape(f, tf.pack([batch_size, time_steps, 1, attn_size]))

    v = get_variable_unsafe('V', [attn_size])
    s = f + y + z
    return tf.reduce_sum(v * tf.tanh(s), [2, 3])


def compute_energy_mixer(hidden, state, *args, **kwargs):
    attn_size = hidden.get_shape()[3].value
    batch_size = tf.shape(hidden)[0]
    time_steps = tf.shape(hidden)[1]

    state = tf.reshape(state, [tf.mul(batch_size, attn_size), 1])
    hidden = tf.transpose(hidden, perm=[1, 0, 2, 3])   # time_steps x batch_size x 1 x attn_size
    hidden = tf.reshape(hidden, tf.pack([time_steps, tf.mul(batch_size, attn_size)]))
    f = tf.matmul(hidden, state)
    f = tf.transpose(f, perm=[1, 0])  # switch time_steps with batch_size
    return f


def global_attention(state, prev_weights, hidden_states, encoder, scope=None, **kwargs):
    with tf.variable_scope(scope or 'attention'):
        # TODO: choose energy function inside config
        compute_energy_ = compute_energy_with_filter if encoder.attention_filters > 0 else compute_energy
        e = compute_energy_(
            hidden_states, state, prev_weights=prev_weights, attention_filters=encoder.attention_filters,
            attention_filter_length=encoder.attention_filter_length, attn_size=encoder.attn_size
        )
        weights = tf.nn.softmax(e)

        shape = tf.shape(weights)
        shape = tf.pack([shape[0], shape[1], 1, 1])

        weighted_average = tf.reduce_sum(tf.reshape(weights, shape) * hidden_states, [1, 2])
        return weighted_average, weights


def local_attention(state, prev_weights, hidden_states, encoder, scope=None, **kwargs):
    """
    Local attention of Luong et al. (http://arxiv.org/abs/1508.04025)
    """
    attn_length = tf.shape(hidden_states)[1]
    state_size = state.get_shape()[1].value

    with tf.variable_scope(scope or 'attention'):
        S = tf.cast(attn_length, dtype=tf.float32)  # source length

        wp = get_variable_unsafe('Wp', [state_size, state_size])
        vp = get_variable_unsafe('vp', [state_size, 1])

        pt = tf.nn.sigmoid(tf.matmul(tf.nn.tanh(tf.matmul(state, wp)), vp))
        pt = tf.floor(S * tf.reshape(pt, [-1, 1]))  # aligned position in the source sentence

        batch_size = tf.shape(state)[0]

        idx = tf.tile(tf.cast(tf.range(attn_length), dtype=tf.float32), tf.pack([batch_size]))
        idx = tf.reshape(idx, [-1, attn_length])

        low = pt - encoder.attention_window_size
        high = pt + encoder.attention_window_size

        mlow = tf.to_float(idx < low)
        mhigh = tf.to_float(idx > high)
        m = mlow + mhigh
        mask = tf.to_float(tf.equal(m, 0.0))

        compute_energy_ = compute_energy_with_filter if encoder.attention_filters > 0 else compute_energy
        e = compute_energy_(
            hidden_states, state, prev_weights=prev_weights, attention_filters=encoder.attention_filters,
            attention_filter_length=encoder.attention_filter_length
        )

        # we have to use this mask thing, because the slice operation
        # does not work with batch dependent indices
        # hopefully softmax is more efficient with sparse vectors
        weights = tf.nn.softmax(e * mask)

        sigma = encoder.attention_window_size / 2
        numerator = -tf.pow((idx - pt), tf.convert_to_tensor(2, dtype=tf.float32))
        div = tf.truediv(numerator, sigma ** 2)

        weights = weights * tf.exp(div)  # result of the truncated normal distribution
        weighted_average = tf.reduce_sum(tf.reshape(weights, [-1, attn_length, 1, 1]) * hidden_states, [1, 2])
        return weighted_average, weights


def attention(state, prev_weights, hidden_states, encoder, **kwargs):
    """
    Proxy for `local_attention` and `global_attention`
    """
    if encoder.attention_window_size > 0:
        attention_ = local_attention
    else:
        attention_ = global_attention

    return attention_(state, prev_weights, hidden_states, encoder, **kwargs)


def multi_attention(state, prev_weights, hidden_states, encoders, **kwargs):
    """
    Same as `attention` except that prev_weights, hidden_states and encoders
    are lists whose length is the number of encoders.
    """
    attns, weights = list(zip(*[
        attention(state, prev_weights_, hidden, encoder, scope='attention_{}'.format(encoder.name))
        for prev_weights_, hidden, encoder in zip(prev_weights, hidden_states, encoders)
    ]))

    return tf.concat(1, attns), list(weights)


def decoder(*args, **kwargs):
    raise NotImplementedError


def attention_decoder(decoder_inputs, initial_state, attention_states, encoders, decoder, decoder_input_length=None,
                      output_projection=None, dropout=None, feed_previous=0.0, **kwargs):
    """
    :param decoder_inputs: tensor of shape (batch_size, output_length)
    :param initial_state: initial state of the decoder (usually the final state of the encoder),
      as a tensor of shape (batch_size, initial_state_size). This state is mapped to the
      correct state size for the decoder.
    :param attention_states: list of tensors of shape (batch_size, input_length, encoder_cell_size),
      usually the encoder outputs (one tensor for each encoder).
    :param encoders: configuration of the encoders
    :param decoder: configuration of the decoder
    :param decoder_input_length:
    :param output_projection: None if no softmax sampling, or tuple (weight matrix, bias vector)
    :param dropout: scalar tensor or None, specifying the keep probability (1 - dropout)
    :param feed_previous: scalar tensor corresponding to the probability to use previous decoder output
      instead of the groundtruth as input for the decoder (1 when decoding, between 0 and 1 when training)
    :return:
      outputs of the decoder as a tensor of shape (batch_size, output_length, decoder_cell_size)
      attention weights as a tensor of shape (output_length, encoders, batch_size, input_length)
    """
    # TODO: dropout instead of keep probability
    assert decoder.cell_size % 2 == 0, 'cell size must be a multiple of 2'   # because of maxout

    if decoder.get('embedding') is not None:
        initializer = decoder.embedding
        embedding_shape = None
    else:
        initializer = None
        embedding_shape = [decoder.vocab_size, decoder.embedding_size]

    with tf.device('/cpu:0'):
        embedding = get_variable_unsafe('embedding_{}'.format(decoder.name), shape=embedding_shape,
                                        initializer=initializer)

    if decoder.use_lstm:
        cell = rnn_cell.BasicLSTMCell(decoder.cell_size, state_is_tuple=False)
        # cell = rnn_cell.LSTMCell(decoder.cell_size, state_is_tuple=False)
    else:
        cell = GRUCell(decoder.cell_size)

    if dropout is not None:
        cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=dropout)

    if decoder.layers > 1:
        cell = MultiRNNCell([cell] * decoder.layers, residual_connections=decoder.residual_connections)

    if output_projection is None:
        output_size = decoder.vocab_size
    else:
        output_size = cell.output_size
        proj_weights = tf.convert_to_tensor(output_projection[0], dtype=tf.float32)
        proj_weights.get_shape().assert_is_compatible_with([cell.output_size, decoder.vocab_size])
        proj_biases = tf.convert_to_tensor(output_projection[1], dtype=tf.float32)
        proj_biases.get_shape().assert_is_compatible_with([decoder.vocab_size])

    with tf.variable_scope('decoder_{}'.format(decoder.name)):
        def extract_argmax_and_embed(prev):
            if output_projection is not None:
                prev = tf.nn.xw_plus_b(prev, output_projection[0], output_projection[1])
            prev_symbol = tf.stop_gradient(tf.argmax(prev, 1))
            emb_prev = tf.nn.embedding_lookup(embedding, prev_symbol)
            return emb_prev

        if embedding is not None:
            time_steps = tf.shape(decoder_inputs)[0]
            batch_size = tf.shape(decoder_inputs)[1]
            flat_inputs = tf.reshape(decoder_inputs, [tf.mul(batch_size, time_steps)])
            flat_inputs = tf.nn.embedding_lookup(embedding, flat_inputs)
            decoder_inputs = tf.reshape(flat_inputs,
                                        tf.pack([time_steps, batch_size, flat_inputs.get_shape()[1].value]))

        attn_lengths = [tf.shape(states)[1] for states in attention_states]

        hidden_states = [tf.expand_dims(states, 2) for states in attention_states]
        attention_ = functools.partial(multi_attention, hidden_states=hidden_states, encoders=encoders)

        input_shape = tf.shape(decoder_inputs)
        time_steps = input_shape[0]
        batch_size = input_shape[1]
        state_size = cell.state_size

        if initial_state is not None:
            if dropout is not None:
                initial_state = tf.nn.dropout(initial_state, dropout)

            state = tf.nn.tanh(
                linear_unsafe(initial_state, state_size, True, scope='initial_state_projection')
            )
        else:
            # if not initial state, initialize with zeroes (this is the case for MIXER)
            state = tf.zeros([batch_size, state_size], dtype=tf.float32)

        sequence_length = decoder_input_length
        if sequence_length is not None:
            sequence_length = tf.to_int32(sequence_length)
            min_sequence_length = tf.reduce_min(sequence_length)
            max_sequence_length = tf.reduce_max(sequence_length)

        time = tf.constant(0, dtype=tf.int32, name='time')

        zero_output = tf.zeros(tf.pack([batch_size, cell.output_size]), tf.float32)

        output_ta = tf.TensorArray(dtype=tf.float32, size=time_steps, clear_after_read=False)

        input_ta = tf.TensorArray(dtype=tf.float32, size=time_steps).unpack(decoder_inputs)
        weights_ta = tf.TensorArray(dtype=tf.float32, size=time_steps)
        weights = [tf.zeros(tf.pack([batch_size, length])) for length in attn_lengths]

        # the dimension of the context vector is the sum of the dimensions of the hidden states from each encoder
        context_size = sum(states_.get_shape()[3].value for states_ in hidden_states)
        attns = tf.zeros(tf.pack([batch_size, context_size]), dtype=tf.float32)
        output = tf.zeros(tf.pack([batch_size, cell.output_size]), dtype=tf.float32)

        def _time_step(time, state, output, _, weights, output_ta, weights_ta):
            input_ = input_ta.read(time)
            # restore some shape information
            r = tf.random_uniform([])
            input_ = tf.cond(tf.logical_and(time > 0, r < feed_previous),
                             lambda: tf.stop_gradient(extract_argmax_and_embed(output_ta.read(time - 1))),
                             lambda: input_)
            input_.set_shape(decoder_inputs.get_shape()[1:])

            # using decoder state instead of decoder output in the attention model seems
            # to give much better results

            context_vector, new_weights = attention_(output, prev_weights=weights)
            weights_ta = weights_ta.write(time, weights)
            x = tf.concat(1, [input_, context_vector])

            call_cell = lambda: unsafe_decorator(cell)(x, state)

            if sequence_length is not None:
                new_output, new_state = rnn._rnn_step(
                    time=time,
                    sequence_length=sequence_length,
                    min_sequence_length=min_sequence_length,
                    max_sequence_length=max_sequence_length,
                    zero_output=zero_output,
                    state=state,
                    call_cell=call_cell,
                    state_size=state_size,
                    skip_conditionals=True)
            else:
                new_output, new_state = call_cell()

            # maxout layer
            output_ = linear_unsafe([new_output, input_, context_vector], decoder.cell_size, True,
                                    scope='maxout')  # U_o, V_o and C_o parameters
            output_ = tf.maximum(*tf.split(1, 2, output_))

            # with tf.device('/cpu:0'):
            output_ = linear_unsafe(output_, output_size, True, scope='output_projection')   # W_o

            output_ta = output_ta.write(time, output_)
            return time + 1, new_state, new_output, context_vector, new_weights, output_ta, weights_ta

        _, _, _, _, _, output_final_ta, weights_final = tf.while_loop(
            cond=lambda time, *_: time < time_steps,
            body=_time_step,
            loop_vars=(time, state, output, attns, weights, output_ta, weights_ta),
            parallel_iterations=decoder.parallel_iterations,
            swap_memory=decoder.swap_memory)

        outputs = output_final_ta.pack()
        # shape (time_steps, encoders, batch_size, input_time_steps)
        weights = tf.slice(weights_final.pack(), [1, 0, 0, 0], [-1, -1, -1, -1])
        return outputs, weights


def beam_search_decoder(decoder_input, initial_state, attention_states, encoders, decoder, output_projection=None,
                        dropout=None, **kwargs):
    """
    Same as `attention_decoder`, except that it only performs one step of the decoder.

    :param decoder_input: tensor of size (batch_size), corresponding to the previous output of the decoder
    :return:
      current output of the decoder
      tuple of (state, new_state, attn_weights, new_attn_weights, attns, new_attns)
    """
    # TODO: code refactoring with `attention_decoder`
    if decoder.get('embedding') is not None:
        embedding_initializer = decoder.embedding
        embedding_shape = None
    else:
        embedding_initializer = None
        embedding_shape = [decoder.vocab_size, decoder.embedding_size]

    with tf.device('/cpu:0'):
        embedding = get_variable_unsafe('embedding_{}'.format(decoder.name),
                                        shape=embedding_shape,
                                        initializer=embedding_initializer)
    if decoder.use_lstm:
        cell = rnn_cell.BasicLSTMCell(decoder.cell_size, state_is_tuple=False)
    else:
        cell = GRUCell(decoder.cell_size)

    if dropout is not None:
        cell = rnn_cell.DropoutWrapper(cell, input_keep_prob=dropout)

    if decoder.layers > 1:
        cell = MultiRNNCell([cell] * decoder.layers, residual_connections=decoder.residual_connections)

    if output_projection is None:
        output_size = decoder.vocab_size
    else:
        output_size = cell.output_size
        proj_weights = tf.convert_to_tensor(output_projection[0], dtype=tf.float32)
        proj_weights.get_shape().assert_is_compatible_with([cell.output_size, decoder.vocab_size])
        proj_biases = tf.convert_to_tensor(output_projection[1], dtype=tf.float32)
        proj_biases.get_shape().assert_is_compatible_with([decoder.vocab_size])

    with tf.variable_scope('decoder_{}'.format(decoder.name)):
        decoder_input = tf.nn.embedding_lookup(embedding, decoder_input)

        attn_lengths = [tf.shape(states)[1] for states in attention_states]
        attn_size = sum(states.get_shape()[2].value for states in attention_states)
        hidden_states = [tf.expand_dims(states, 2) for states in attention_states]
        attention_ = functools.partial(multi_attention, hidden_states=hidden_states, encoders=encoders)
        batch_size = tf.shape(decoder_input)[0]
        state_size = cell.state_size

        # if initial_state is not None:
        if dropout is not None:
            initial_state = tf.nn.dropout(initial_state, dropout)

        state = tf.nn.tanh(
            linear_unsafe(initial_state, cell.state_size, True, scope='initial_state_projection')
        )

        weights = [tf.zeros(tf.pack([batch_size, length])) for length in attn_lengths]
        # attns = tf.zeros(tf.pack([batch_size, attn_size]), dtype=tf.float32)
        output = tf.zeros(tf.pack([batch_size, cell.output_size]), dtype=tf.float32)

        context_vector, new_weights = attention_(output, prev_weights=weights)
        x = tf.concat(1, [decoder_input, context_vector])
        new_output, new_state = unsafe_decorator(cell)(x, state)

        new_output = linear_unsafe([new_output, decoder_input, context_vector], decoder.cell_size, True,
                                scope='maxout')
        new_output = tf.maximum(*tf.split(1, 2, new_output))
        new_output = linear_unsafe(new_output, output_size, True, scope='output_projection')
        # with tf.variable_scope('attention_output_projection'):
        #    output = linear_unsafe([cell_output, new_attns], output_size, True)

        beam_tensors = namedtuple('beam_tensors', 'state new_state prev_output')
        return new_output, beam_tensors(state, new_state, output)


def sequence_loss(logits, targets, weights, average_across_timesteps=True, average_across_batch=True,
                  softmax_loss_function=None):
    time_steps = tf.shape(targets)[0]
    batch_size = tf.shape(targets)[1]

    logits_ = tf.reshape(logits, tf.pack([time_steps * batch_size, logits.get_shape()[2].value]))
    targets_ = tf.reshape(targets, tf.pack([time_steps * batch_size]))

    if softmax_loss_function is None:
        crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(logits_, targets_)
    else:
        crossent = softmax_loss_function(logits_, targets_)

    crossent = tf.reshape(crossent, tf.pack([time_steps, batch_size]))
    log_perp = tf.reduce_sum(crossent * weights, 0)

    if average_across_timesteps:
        total_size = tf.reduce_sum(weights, 0)
        total_size += 1e-12  # just to avoid division by 0 for all-0 weights
        log_perp /= total_size

    cost = tf.reduce_sum(log_perp)

    if average_across_batch:
        batch_size = tf.shape(targets)[1]
        return cost / tf.cast(batch_size, tf.float32)
    else:
        return cost
