import numpy as np
import pandas as pd
import datetime as dt
from dateutil.relativedelta import relativedelta
import ast
import argparse

import os
from hyperopt import fmin, tpe, STATUS_OK, Trials
from tensorflow.python.keras import callbacks, optimizers
from tensorflow.python.keras.models import Model
from tensorflow.python.keras.layers import Dense, Dropout, Input, Concatenate
from tensorflow.python.keras import backend as K
from sklearn.metrics import r2_score, mean_absolute_error

from sqlalchemy import create_engine
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
    input_shape2 = (X_train_s.shape[-1],)
    input_img = Input(shape=input_shape)
    input_img2 = Input(shape=input_shape2)

    init_nodes = params['init_nodes']
    nodes_mult = params['nodes_mult']
    mult_freq = params['mult_freq']
    mult_start = params['mult_start']
    num_Dense_layer = params['num_Dense_layer']

    d_1 = Dense(init_nodes, activation=params['activation'])(input_img)  # remove kernel_regularizer=regularizers.l1(params['l1'])
    d_1 = Dropout(params['dropout'])(d_1)

    # dense model 1: for top n features
    nodes = [init_nodes]
    for i in range(1, num_Dense_layer):
        temp_nodes = int(min(init_nodes * (2 ** (nodes_mult * max((i - mult_start+3)//mult_freq, 0))), 32)) # nodes grow at 2X or stay same - at most 128 nodes
        d_1 = Dense(temp_nodes, activation=params['activation'])(d_1)  # remove kernel_regularizer=regularizers.l1(params['l1'])
        nodes.append(temp_nodes)

        if i != num_Dense_layer - 1:    # last dense layer has no dropout
            d_1 = Dropout(params['dropout'])(d_1)

    # dense model 2: for stock_return
    d_2 = Dense(2, activation=params['activation'])(input_img2)
    d_2 = Dense(2, activation=params['activation'])(d_2)

    print(nodes)
    sql_result['num_nodes'] = str(nodes)

    f_x = Concatenate(axis=1)([d_1, d_2])
    f_x = Dense(1)(d_1)

    callbacks_list = [callbacks.ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=10),
                      callbacks.EarlyStopping(monitor='val_loss', patience=10, mode='auto')]  # add callbacks
    lr_val = 10 ** -int(params['learning_rate'])

    adam = optimizers.Adam(lr=lr_val)
    model = Model([input_img, input_img2], f_x)
    model.compile(adam, loss='mae')
    model.summary()

    history = model.fit([X_train, X_train_s], Y_train, epochs=50, batch_size=params['batch_size'], validation_data=(X_valid, Y_valid),
                        callbacks=callbacks_list, verbose=1)

    Y_test_pred = model.predict([X_test, X_test_s])
    Y_train_pred = model.predict([X_train, X_train_s])
    Y_valid_pred = model.predict([X_valid, X_valid_s])

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
        hpot['best_trial'] = sql_result['trial_lgbm']

    K.clear_session()
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

    # plot_history(hpot['best_history'])  # plot training history

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

    plt.savefig('results_dense/plot_dense_{}_{}.png'.format(hpot['best_trial'], hpot['best_mae']))
    plt.close()

def pred_to_sql(Y_test_pred):
    ''' prepare array Y_test_pred to DataFrame ready to write to SQL '''

    df = pd.DataFrame()
    df['identifier'] = test_id
    df['pred'] = Y_test_pred
    df['trial_lgbm'] = [sql_result['trial_lgbm']] * len(test_id)
    df['name'] = [sql_result['name']] * len(test_id)
    # print('stock-wise prediction: ', df)

    return df

if __name__ == "__main__":

    sql_result = {}
    hpot = {}
    use_median = True
    chron_valid = False
    qcut_q = 10
    sql_result['y_type'] = 'ibes'
    period_1 = dt.datetime(2013,3,31)

    parser = argparse.ArgumentParser()
    parser.add_argument('--sp_only', default=False, action='store_true')
    parser.add_argument('--exclude_fwd', default=False, action='store_true')
    parser.add_argument('--resume', default=False, action='store_true')
    parser.add_argument('--num_best_col', type=int, default=0)
    parser.add_argument('--icb_code', type=int, default=0)
    parser.add_argument('--sample_no', type=int, default=21)
    parser.add_argument('--name_sql', required=True)
    args = parser.parse_args()

    # default settings
    exclude_fwd = args.exclude_fwd
    ibes_qcut_as_x = not(args.exclude_fwd)

    db_last_param, sql_result = read_db_last(sql_result, 'results_dense2')  # update sql_result['trial_hpot'/'trial_lgbm'] & got params for resume (if True)
    data = load_data(macro_monthly=True, sp_only=args.sp_only)          # load all data: create load_data.main = df for all samples - within data(CLASS)

    indi_industry_new = [11, 20, 30, 35, 40, 45, 51, 60, 65]

    for add_ind_code in [args.icb_code]: # 1 means add industry code as X
        data.split_industry(add_ind_code, combine_ind=True)
        sql_result['icb_code'] = add_ind_code

        for i in tqdm(range(args.sample_no)):  # roll over testing period
            testing_period = period_1 + i * relativedelta(months=3)
            sql_result['testing_period'] = testing_period

            if args.resume == True:     # resume from last records in DB
                if {'icb_code': add_ind_code, 'testing_period': pd.Timestamp(testing_period)} == db_last_param:  # if current loop = last records
                    args.resume = False
                    print('---------> Resume Training', add_ind_code, testing_period)
                else:
                    print('Not yet resume: params done', add_ind_code, testing_period)
                    continue

            sample_set_s, cut_bins, cv, test_id, feature_names = data.split_all(testing_period, qcut_q,
                                                                                y_type=sql_result['y_type'],
                                                                                exclude_fwd=exclude_fwd,
                                                                                use_median=use_median,
                                                                                chron_valid=chron_valid,
                                                                                # num_best_col=n)
                                                                                num_best_col=args.num_best_col,
                                                                                filter_stock_return_only=True)

            sample_set, cut_bins, cv, test_id, feature_names = data.split_all(testing_period, qcut_q,
                                                                              y_type=sql_result['y_type'],
                                                                              exclude_fwd=exclude_fwd,
                                                                              use_median=use_median,
                                                                              chron_valid=chron_valid,
                                                                              # num_best_col=n)
                                                                              num_best_col=args.num_best_col,
                                                                              filter_stock_return_only=False)

            print(feature_names)
            sql_result['name'] = '{} -code {} -exclude_fwd {}'.format(args.name_sql, args.icb_code, args.exclude_fwd)

            X_test = np.nan_to_num(sample_set['test_x'], nan=0)
            Y_test = sample_set['test_y']
            X_test_s = np.nan_to_num(sample_set_s['test_x'], nan=0)
            Y_test_s = sample_set_s['test_y']

            sql_result['number_features'] = X_test.shape[1]

            cv_number = 1
            for train_index, test_index in cv:
                sql_result['cv_number'] = cv_number

                X_train = np.nan_to_num(sample_set['train_x'][train_index], nan=0)
                Y_train = sample_set['train_y'][train_index]
                X_valid =  np.nan_to_num(sample_set['train_x'][test_index], nan=0)
                Y_valid = sample_set['train_y'][test_index]

                X_train_s = np.nan_to_num(sample_set_s['train_x'][train_index], nan=0)
                Y_train_s = sample_set_s['train_y'][train_index]
                X_valid_s = np.nan_to_num(sample_set_s['train_x'][test_index], nan=0)
                Y_valid_s = sample_set_s['train_y'][test_index]

                print(X_train.shape , Y_train.shape, X_valid.shape, Y_valid.shape, X_test.shape, Y_test.shape)
                space = find_hyperspace(sql_result)
                HPOT(space, 10)

                sql_result['trial_hpot'] += 1
                cv_number += 1


