from keras.engine import Model
from keras.layers import Layer, Bidirectional, TimeDistributed, \
    Dense, LSTM, Masking, Input, RepeatVector, Dropout, Convolution1D, \
    BatchNormalization, MaxPool1D, Flatten, Activation, Reshape, Lambda
from keras.layers.merge import concatenate, add
import keras.backend as K
from data_provider import NUM_CDR_FEATURES, NUM_AG_FEATURES


AG_RNN_STATE_SIZE = 128
AB_RNN_STATE_SIZE = 128
CONV_FILTERS = 32
CONV_FILTER_SPAN = 9


def false_neg(y_true, y_pred):
    return K.squeeze(K.clip(y_true - K.round(y_pred), 0.0, 1.0), axis=-1)


def false_pos(y_true, y_pred):
    return K.squeeze(K.clip(K.round(y_pred) - y_true, 0.0, 1.0), axis=-1)


# Should probably triple-check that it works as expected
class MaskingByLambda(Layer):
    def __init__(self, func, **kwargs):
        self.supports_masking = True
        self.mask_func = func
        super(MaskingByLambda, self).__init__(**kwargs)

    def compute_mask(self, input, input_mask=None):
        return self.mask_func(input, input_mask)

    def call(self, x, mask=None):
        exd_mask = K.expand_dims(self.mask_func(x, mask), axis=-1)
        return x * K.cast(exd_mask, K.floatx())


# Base masking decision only on the first elements
def ab_mask(input, mask):
    return K.any(K.not_equal(input[:, :, :AB_RNN_STATE_SIZE], 0.0), axis=-1)


def ag_mask(input, mask):
    return K.any(K.not_equal(input[:, :, :AG_RNN_STATE_SIZE], 0.0), axis=-1)


# 1D convolution that supports masking by retaining the mask of the input
class MaskedConvolution1D(Convolution1D):
    def __init__(self, *args, **kwargs):
        self.supports_masking = True
        assert kwargs['padding'] == 'same' # Only makes sense for 'same'
        super(MaskedConvolution1D, self).__init__(*args, **kwargs)

    def compute_mask(self, input, input_mask=None):
        return input_mask

    def call(self, x, mask=None):
        assert mask is not None
        mask = K.expand_dims(mask, axis=-1)
        x = super(MaskedConvolution1D, self).call(x)
        return x * K.cast(mask, K.floatx())


class TNet(Layer):
    def __init__(self, localisation_net, point_dim, orthog_loss=0.0,
                 **kwargs):
        self.loc_net = localisation_net
        self.orthog_loss = orthog_loss
        self.point_dim = point_dim
        super().__init__(**kwargs)

    def orthogonal_regularisation_loss(self, mat):
        # Assuming mat is square
        mat_trans = K.permute_dimensions(mat, (0, 2, 1))
        return self.orthog_loss * K.sum(
            K.square(K.eye(self.point_dim) - K.batch_dot(mat, mat_trans)))

    def call(self, x):
        # Could verify shapes here
        transform_mat = self.loc_net(x)
        if self.orthog_loss > 0.0:
            self.add_loss(self.orthogonal_regularisation_loss(transform_mat),
                          inputs=[x])

        result = K.batch_dot(x, transform_mat)
        return result


def loc_net(max_points, point_dim):
    input_points = Input(shape=(max_points, point_dim))

    # Transform each point to get 1024 features
    x = Convolution1D(64, 1, padding='valid', activation='elu')(input_points)
    x = Convolution1D(128, 1, padding='valid', activation='elu')(x)
    x = Convolution1D(1024, 1, padding='valid', activation='elu')(x)

    # Select max of all points
    x = MaxPool1D(pool_size=max_points)(x)
    x = Flatten()(x) # Remove the point dimension

    # Use FCs to reduce to the transformation matrix
    x = Dense(512, activation='elu')(x)
    x = Dense(256, activation='elu')(x)
    x = Dense(point_dim * point_dim)(x)

    # Add an identity matrix
    flat_eye = K.reshape(K.eye(point_dim), (-1, ))
    x = Lambda(lambda x: x + flat_eye)(x)
    transform_mat = Reshape((point_dim, point_dim))(x)
    return Model(inputs=input_points, outputs=transform_mat)


def point_net(max_points):
    input_pts = Input(shape=(max_points, 3))
    trans_pts = TNet(loc_net(max_points, 3), 3)(input_pts)

    # Note: Not entirely sure what PointNet authors meant here
    x = Convolution1D(64, 1, padding='valid', activation='elu')(trans_pts)
    x = Convolution1D(64, 1, padding='valid', activation='elu')(x)

    local_fts = TNet(loc_net(max_points, 64), 64, orthog_loss=0.001)(x)

    x = Convolution1D(64, 1, padding='valid', activation='elu')(local_fts)
    x = Convolution1D(128, 1, padding='valid', activation='elu')(x)
    x = Convolution1D(1024, 1, padding='valid', activation='elu')(x)

    x = MaxPool1D(pool_size=max_points)(x)
    global_feat = Flatten()(x) # Remove the point dimension

    return Model(inputs=input_pts, outputs=global_feat)


def get_model(max_ag_len, max_cdr_len, max_ag_atoms, max_cdr_atoms):
    input_ag = Input(shape=(max_ag_len, NUM_AG_FEATURES))
    input_ag_m = Masking()(input_ag)

    input_ag_atoms = Input(shape=(max_ag_atoms, 3))
    global_ag_feat = point_net(max_ag_atoms)(input_ag_atoms)
    global_ag_feat = RepeatVector(max_ag_len)(global_ag_feat)
    ag = concatenate([input_ag_m, global_ag_feat])
    ag_m = MaskingByLambda(ag_mask)(ag)

    enc_ag = Bidirectional(LSTM(AG_RNN_STATE_SIZE,
                                dropout=0.2,
                                recurrent_dropout=0.1),
                           merge_mode='concat')(ag_m)

    input_ab = Input(shape=(max_cdr_len, NUM_CDR_FEATURES))
    input_ab_m = Masking()(input_ab)

    input_ab_atoms = Input(shape=(max_cdr_atoms, 3))
    global_ab_feat = point_net(max_cdr_atoms)(input_ab_atoms)
    global_ab_feat = RepeatVector(max_cdr_len)(global_ab_feat)
    ab = concatenate([input_ab_m, global_ab_feat])
    ab_m = MaskingByLambda(ab_mask)(ab)

    # Adding recurrent dropout here is a bad idea
    # --- sequences are very short
    ab_net_out = Bidirectional(LSTM(AB_RNN_STATE_SIZE,
                                    return_sequences=True),
                               merge_mode='concat')(ab_m)

    enc_ag_rep = RepeatVector(max_cdr_len)(enc_ag)
    ab_ag_repr = concatenate([ab_net_out, enc_ag_rep])
    ab_ag_repr = MaskingByLambda(ab_mask)(ab_ag_repr)
    ab_ag_repr = Dropout(0.2)(ab_ag_repr)

    aa_probs = TimeDistributed(Dense(1, activation='sigmoid'))(ab_ag_repr)
    model = Model(inputs=[input_ag, input_ag_atoms, input_ab, input_ab_atoms],
                  outputs=aa_probs)
    model.compile(loss='binary_crossentropy',
                  optimizer='adam',
                  metrics=['binary_accuracy', false_pos, false_neg],
                  sample_weight_mode="temporal")
    return model


def baseline_model(max_cdr_len):
    input_ab = Input(shape=(max_cdr_len, NUM_CDR_FEATURES))
    input_ab_m = Masking()(input_ab)

    aa_probs = TimeDistributed(Dense(1, activation='sigmoid'))(input_ab_m)
    model = Model(inputs=input_ab, outputs=aa_probs)
    model.compile(loss='binary_crossentropy',
                  optimizer='adam',
                  metrics=['binary_accuracy', false_pos, false_neg],
                  sample_weight_mode="temporal")
    return model


def ab_only_model(max_cdr_len):
    input_ab = Input(shape=(max_cdr_len, NUM_CDR_FEATURES))
    input_ab_m = Masking()(input_ab)

    # Adding dropout_U here is a bad idea --- sequences are very short and
    # all information is essential
    ab_net_out = Bidirectional(LSTM(AB_RNN_STATE_SIZE, return_sequences=True),
                               merge_mode='concat')(input_ab_m)

    ab_ag_repr = Dropout(0.1)(ab_net_out)
    aa_probs = TimeDistributed(Dense(1, activation='sigmoid'))(ab_ag_repr)
    model = Model(inputs=input_ab, outputs=aa_probs)
    model.compile(loss='binary_crossentropy',
                  optimizer='adam',
                  metrics=['binary_accuracy', false_pos, false_neg],
                  sample_weight_mode="temporal")
    return model
