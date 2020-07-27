import numpy as np
import pandas as pd
import datetime as dt
import ast
import os
from hyperopt import fmin, tpe, hp, STATUS_OK, Trials
from tensorflow.python.keras import callbacks, optimizers, regularizers
from tensorflow.python.keras.models import Model
from tensorflow.python.keras.layers import Dense, Dropout, Input
from sklearn.metrics import r2_score, mean_absolute_error

from sqlalchemy import create_engine
from dateutil.relativedelta import relativedelta
from tqdm import tqdm

from load_data_lgbm import load_data
from hyperspace_dense import find_hyperspace
from LightGBM import read_db_last
import matplotlib.pyplot as plt

import tensorflow as tf                             # avoid error in Tensorflow initialization
tf.compat.v1.disable_eager_execution()
os.environ['TF_FORCE_GPU_ALLOW_GROWTH'] = 'true'

db_string = 'postgres://postgres:DLvalue123@hkpolyu.cgqhw7rofrpo.ap-northeast-2.rds.amazonaws.com:5432/postgres'
engine = create_engine(db_string)

def dense_train(space):
    ''' train lightgbm booster based on training / validaton set -> give predictions of Y '''

    params = space.copy()
    print(params)

    input_shape = (X_train.shape[-1],)      # input shape depends on x_fields used
    input_img = Input(shape=input_shape)

    init_nodes = params['init_nodes']
    nodes_mult = params['nodes_mult']
    mult_freq = params['mult_freq']
    mult_start = params['mult_start']
    num_Dense_layer = params['num_Dense_layer']

    # nodes_list = ast.literal_eval(params['num_nodes'])  # convert str to nested dictionary
    # print(nodes_list)
    # num_Dense_layer = len(nodes_list)

    nodes = []
    for i in range(num_Dense_layer):
        # temp_nodes = nodes_list[i]
        temp_nodes = int(min(init_nodes * (2 ** (nodes_mult * max((i - mult_start+3)//mult_freq, 0))), 16)) # nodes grow at 2X or stay same - at most 128 nodes
        d_1 = Dense(temp_nodes, activation=params['activation'])(input_img) # remove kernel_regularizer=regularizers.l1(params['l1'])
        nodes.append(temp_nodes)

        if i != num_Dense_layer - 1:    # last dense layer has no dropout
            d_1 = Dropout(params['dropout'])(d_1)

    f_x = Dense(1)(d_1)

    print(nodes)
    sql_result['num_nodes'] = str(nodes)

    callbacks_list = [callbacks.ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=10),
                      callbacks.EarlyStopping(monitor='val_loss', patience=50, mode='auto')]  # add callbacks
    lr_val = 10 ** -int(params['learning_rate'])

    adam = optimizers.Adam(lr=lr_val)
    model = Model(input_img, f_x)
    model.compile(adam, loss='mae')

    history = model.fit(X_train, Y_train, epochs=200, batch_size=params['batch_size'], validation_data=(X_valid, Y_valid),
                        callbacks=callbacks_list, verbose=1)
    model.summary()

    # train_mae = model.evaluate(X_train, Y_train,  verbose=1)
    # valid_mae = model.evaluate(X_valid, Y_valid, verbose=1)
    # test_mae = model.evaluate(X_test, Y_test, verbose=1)
    Y_test_pred = model.predict(X_test)
    Y_train_pred = model.predict(X_train)
    Y_valid_pred = model.predict(X_valid)

    return Y_test_pred, Y_train_pred, Y_valid_pred, history

def eval(space):
    ''' train & evaluate each of the dense model '''

    Y_test_pred, Y_train_pred, Y_valid_pred, history = dense_train(space)

    result = {'mae_train': mean_absolute_error(Y_train, Y_train_pred),
              'mae_valid': mean_absolute_error(Y_valid, Y_valid_pred),
              'mae_test': mean_absolute_error(Y_test, Y_test_pred),
              'r2_train': r2_score(Y_train, Y_train_pred),
              'r2_valid': r2_score(Y_valid, Y_valid_pred),
              'r2_test': r2_score(Y_test, Y_test_pred),
              'status': STATUS_OK}

    sql_result.update(space)
    sql_result.update(result)
    sql_result['finish_timing'] = dt.datetime.now()

    print('sql_result_before writing: ', sql_result)
    hpot['all_results'].append(sql_result.copy())

    if result['mae_valid'] < hpot['best_mae']:  # update best_mae to the lowest value for Hyperopt
        hpot['best_mae'] = result['mae_valid']
        hpot['best_stock_df'] = pred_to_sql(Y_test_pred)
        hpot['best_history'] = history

    sql_result['trial_lgbm'] += 1

    return result['mae_valid']

def HPOT(space, max_evals = 10):
    ''' use hyperopt on each set '''

    hpot['best_mae'] = 1  # record best training (min mae_valid) in each hyperopt
    hpot['all_results'] = []

    trials = Trials()
    best = fmin(fn=eval, space=space, algo=tpe.suggest, max_evals=max_evals, trials=trials)

    print(hpot['best_stock_df'])

    with engine.connect() as conn:
        pd.DataFrame(hpot['all_results']).to_sql('results_dense2', con=conn, index=False, if_exists='append', method='multi')
        hpot['best_stock_df'].to_sql('results_dense2_stock', con=conn, index=False, if_exists='append', method='multi')
    engine.dispose()

    plot_history(hpot['best_history'])  # plot training history

    sql_result['trial_hpot'] += 1

    return best

def plot_history(history):
    ''' plot the training loss history '''

    history_dict = history.history
    epochs = range(10, len(history_dict['loss'])+1)

    plt.plot(epochs, history_dict['loss'][9:], 'bo', label='training loss')
    plt.plot(epochs, history_dict['val_loss'][9:], 'b', label='validation loss')
    plt.title('dense - training and validation loss')
    plt.xlabel('epochs')
    plt.ylabel('loss')
    plt.legend()

    plt.savefig('results_dense/plot_dense_{}.png'.format(hpot['best_mae']))
    plt.close()

def pred_to_sql(Y_test_pred):
    ''' prepare array Y_test_pred to DataFrame ready to write to SQL '''

    df = pd.DataFrame()
    df['identifier'] = test_id
    df['pred'] = Y_test_pred
    df['trial_lgbm'] = [sql_result['trial_lgbm']] * len(test_id)
    # print('stock-wise prediction: ', df)

    return df

if __name__ == "__main__":

    sql_result = {}
    hpot = {}

    # default settings to
    exclude_fwd = True
    use_median = True
    chron_valid = False
    ibes_qcut_as_x = False
    qcut_q = 10
    sql_result['y_type'] = 'ibes'

    # these are parameters used to load_data
    period_1 = dt.datetime(2013,3,31)
    sample_no = 25
    sql_result['name'] = 'with ind code -small space'

    db_last_param, sql_result = read_db_last(sql_result, 'results_dense2')  # update sql_result['trial_hpot'/'trial_lgbm'] & got params for resume (if True)
    data = load_data(macro_monthly=True)

    for add_ind_code in [0, 1, 2]: # 1 means add industry code as X
        data.split_entire(add_ind_code=add_ind_code)
        sql_result['icb_code'] = add_ind_code

        for i in tqdm(range(sample_no)):  # roll over testing period
            testing_period = period_1 + i * relativedelta(months=3)
            sql_result['testing_period'] = testing_period

            # if qcut_q==10:
            try:
                sample_set, cut_bins, cv, test_id, feature_names = data.split_all(testing_period, qcut_q,
                                                                                  y_type=sql_result['y_type'],
                                                                                  exclude_fwd=exclude_fwd,
                                                                                  use_median=use_median,
                                                                                  chron_valid=chron_valid)

                print(feature_names)

                X_test = np.nan_to_num(sample_set['test_x'], nan=0)
                Y_test = sample_set['test_y']

                cv_number = 1
                for train_index, test_index in cv:
                    sql_result['cv_number'] = cv_number

                    X_train = np.nan_to_num(sample_set['train_x'][train_index], nan=0)
                    Y_train = sample_set['train_y'][train_index]
                    X_valid =  np.nan_to_num(sample_set['train_x'][test_index], nan=0)
                    Y_valid = sample_set['train_y'][test_index]

                    print(X_train.shape , Y_train.shape, X_valid.shape, Y_valid.shape, X_test.shape, Y_test.shape)
                    space = find_hyperspace(sql_result)
                    HPOT(space, 10)
                    cv_number += 1
            except:
                continue


