from sqlalchemy import create_engine, text
import numpy as np
import pandas as pd
import xarray
from sqlalchemy import create_engine

import datetime as dt
from dateutil.relativedelta import relativedelta
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.model_selection import GroupShuffleSplit
from collections import Counter
import gc
from tqdm import tqdm

from miscel import check_dup, date_type
from preprocess.ratios import worldscope, full_period, trim_outlier

db_string = 'postgres://postgres:DLvalue123@hkpolyu.cgqhw7rofrpo.ap-northeast-2.rds.amazonaws.com:5432/postgres'
engine = create_engine(db_string)

idd = 'C156E0340'
def check_id(df, id=idd):
    with pd.option_context('display.max_rows', None, 'display.max_columns', None):  # more options can be specified also
        print(df.loc[df['identifier'] ==id, ['period_end', 'y_ibes']].sort_values(['period_end']))
    exit(0)

def read_data(macro_monthly=True):

    ''' read worldscope_quarter_summary / ibes_data / stock_data / macro_data / clean_ratios'''

    try:  # read Worldscope Data after cleansing
        ws = pd.read_csv('preprocess/quarter_summary_clean.csv')    # raw worldscope data (i.e. point-in-time)
        ibes = pd.read_csv('preprocess/ibes_data.csv')              # raw ibes data
        stock = pd.read_csv('preprocess/stock_data.csv')            # raw stock return data
        macro = pd.read_csv('preprocess/clean_macros.csv')          # clean macro data (i.e. yoy)
        new_macro = pd.read_csv('preprocess/clean_macros_new.csv')          # clean macro data (i.e. yoy)
        y = pd.read_csv('preprocess/clean_ratios.csv', usecols=['identifier','period_end','y_ibes','y_ni'])     # Y ratios from clean table
        print('local version run - quarter_summary_clean / ibes_data / stock_data / macro_data / clean_ratios')
    except:
        print('---------------------> load rnn data')
        ws = worldscope().fill_missing_ws() # from Proprocess.ratios.py genenrate raw worldscope data
        with engine.connect() as conn:
            ibes = pd.read_sql('SELECT * FROM ibes_data', conn)     # use DB TABLE if no local file
            stock = pd.read_sql('SELECT * FROM stock_data', conn)
            macro = pd.read_sql('SELECT * FROM clean_macros', conn)
            new_macro = pd.read_sql('SELECT * FROM clean_macros_new', conn)
            y = pd.read_sql('SELECT identifier, period_end, y_ibes, y_ni FROM clean_ratios', conn)
        engine.dispose()

    if macro_monthly == True:
        non_replace_col = list(set(macro.columns.to_list()) - set(new_macro.columns.to_list()))
        macro = new_macro.merge(macro[['period_end'] + non_replace_col], on='period_end', how='left')

    ibes_stock = pd.merge(date_type(ibes), date_type(stock), on=['identifier','period_end'])   # merge ibes / stock data (both originally labeled with tickers)
    ibes_stock = ibes_stock.groupby(['identifier', 'period_end']).mean().reset_index(drop=False)  # for cross listing use average

    ibes_stock = full_period(ibes_stock)                                # stock data pushing ahead 1Q
    ibes_stock['close'] = ibes_stock['close'].shift(-1)
    ibes_stock.loc[ibes_stock.groupby('identifier').tail(1).index, 'close'] = np.nan

    main = pd.merge(date_type(ws), ibes_stock, on=['identifier','period_end'], how='left')  # convert ws to yoy
    main.columns = [x.lower() for x in main.columns]    # convert columns name to lower case
    main = yoy(main)   # convert raw point-in-time data to yoy formats
    macro = macro.loc[date_type(macro)['period_end'] >= dt.datetime(1997, 12, 31)]  # filter records after 1998
    main = add_macro(main, macro).map_macros()  # add clean macro variables
    main = main.merge(date_type(y), on=['identifier','period_end'], how='left')
    main.columns = [x.lower() for x in main.columns]    # convert columns name to lower case
    main = main.sort_values('market').drop_duplicates(['identifier','period_end'], keep='first')    # for cross listing (CH + HK) use macro for CH

    return main

class add_macro:
    ''' combine macros ratios and other ratios (worldscope / ibes / stock / Y) '''

    def __init__(self, ratios, macros):
        self.ratios = date_type(self.label_nation_sector(ratios))
        self.macros = date_type(macros)

    def label_nation_sector(self, ratios):
        ''' read files mapping icb_code & market '''

        with engine.connect() as conn:
            markets = pd.read_sql("SELECT index_ric, icb_sector, identifier FROM dl_value_universe "
                                  "WHERE identifier IS NOT NULL", conn)
        engine.dispose()

        ratios = pd.merge(ratios, markets, on=['identifier'])
        ratios['market'] = ratios['index_ric'].replace(['0#.CSI300', '0#.N225', '0#.SPX', '0#.HSLI'], ['CH','JP','US','HK'])
        return ratios.drop(['index_ric'], axis=1)

    def map_macros(self):
        ''' map macros to ratios TABLE '''

        with engine.connect():
            mapping = pd.read_sql('SELECT * FROM macro_formula', engine).iloc[:,:3]  # TABLE macro_formula map macros variables -> worldwise / specific market
        engine.dispose()

        # map worldwide indicators based on period_end
        worldwide_col = mapping.loc[mapping['market'] == 'W', 'symbol'].to_list()  # worldwide macros
        self.ratios = pd.merge(self.ratios, self.macros[['period_end'] + worldwide_col], on=['period_end'], how='left')

        # map market-specific indicators based on period_end & market
        new_macros = self.macros.set_index(['period_end']).unstack().reset_index() # pivot -> index (period_end, market)
        new_macros.columns = ['symbol', 'period_end', 'values']
        new_macros = pd.merge(new_macros, mapping, on=['symbol'])
        new_macros = new_macros.loc[new_macros['market'] != 'W']    # market-specific ratios
        new_macros = new_macros.pivot_table(index=['period_end','market'], columns='type', values='values')
        self.ratios = pd.merge(self.ratios, new_macros, on=['period_end', 'market'], how='left')

        self.ratios['period_end'] = pd.to_datetime(self.ratios['period_end'])
        return self.ratios

def yoy(df):
    ''' calculate yoy for ws / ibes / stock data '''

    ws_col = ['cap1fd12', 'ebd1fd12', 'eps1fd12', 'eps1tr12', 'close', 'fn_18100', 'fn_18158', 'fn_18199', 'fn_18262',
              'fn_18263', 'fn_18264', 'fn_18265', 'fn_18266', 'fn_18267', 'fn_18269', 'fn_18271', 'fn_18304', 'fn_18308',
              'fn_18309', 'fn_18310', 'fn_18311', 'fn_18312', 'fn_18313', 'fn_2001', 'fn_2101', 'fn_2201', 'fn_2501',
              'fn_2999', 'fn_3101', 'fn_3255', 'fn_3501', 'fn_5085', 'fn_8001']     # replace fn_18263 (ws net income) with eps1tr12 (ibes eps)

    df = full_period(df)  # fill in non sequential records

    df['eps_rnn'] = (df['eps1tr12'] - df['eps1tr12'].shift(4))/(df['fn_8001'].shift(4))*(df['fn_5192'].shift(4))
    df.loc[df.groupby('identifier').head(4).index, 'eps_rnn'] = np.nan     # avoid calculation with different identifier

    # df[['identifier', 'period_end', 'eps_rnn', 'eps1tr12', 'fn_8001', 'fn_5192']].to_csv('#simple_rnn1.csv')
    # exit(0)

    df[ws_col] = (df[ws_col] / df[ws_col].shift(4)).sub(1)  # calculate YoY using (T0 - T-4)/T-4
    df.loc[df.groupby('identifier').head(4).index, ws_col] = np.nan     # avoid calculation with different identifier

    df[ws_col] = trim_outlier(df[ws_col])   # use 100% as maximum values -> avoid inf
    df = df.dropna(subset=ws_col, how='all')
    # print(df.describe().T[['min','max']])

    return df.filter(['identifier', 'period_end'] + ws_col + ['eps_rnn'])

class load_data:
    ''' main function:
        1. split train + valid + test -> sample set
        2. convert x with standardization, y with qcut '''

    def __init__(self, macro_monthly):
        ''' split train and testing set
                    -> return dictionary contain (x, y, y without qcut) & cut_bins'''

        self.main = read_data(macro_monthly)     # all YoY ratios

        # print('check inf: ', np.any(np.isinf(self.main.drop(['identifier', 'period_end', 'icb_sector', 'market'], axis=1).values)))

        # self.main[['identifier', 'period_end', 'y_ibes', 'eps_rnn']].to_csv('#simple_rnn.csv')
        # exit(0)

        # define self objects
        self.cut_bins = {}
        self.sector = pd.DataFrame()
        self.train = pd.DataFrame()
        self.main = self.main.dropna(subset=['icb_sector'])

    def split_entire(self, add_ind_code):   # we always use entire samples for training
        ''' train on all sample, add_ind_code = True means adding industry_code(2) as x '''

        self.main['icb_industry'] = self.main['icb_sector'].astype(str).str[:2].astype(int)

        if add_ind_code == 1:   # add industry code as X
            self.main['icb_industry_x'] = self.main['icb_industry']
        elif add_ind_code == 2:   # add industry code as X
            self.main['icb_sector_x'] = self.main['icb_sector']
        elif add_ind_code == 0:
            pass
        else:
            print('wrong add_ind_code')
            exit(1)

        self.sector = self.main

    def split_train_test(self, testing_period, qcut_q, y_type, exclude_fwd=False, small_training=True, eps_only=False):
        ''' split training / testing set based on testing period '''

        # 1. split and qcut train / test Y
        start_train_y = testing_period - relativedelta(years=10)    # train df = 40 quarters
        self.sector = full_period(self.sector).sort_values(['period_end', 'identifier']).reset_index(drop=True)  # fill in for non-sequential records

        # self.sector.loc[
        #     (start_train_y <= self.sector['period_end']) &  # extract array for 10y Y records for training set
        #     (self.sector['period_end'] < testing_period)][['identifier', 'period_end', 'y_ibes', 'eps1fd12', 'fn_8001']].to_csv('rnn_training_len.csv', index=False)
        # exit(0)

        train_y = self.sector.loc[(start_train_y <= self.sector['period_end']) &    # extract array for 10y Y records for training set
                                  (self.sector['period_end'] < testing_period)]['y_{}'.format(y_type)]
        train_filter = self.sector.loc[(start_train_y <= self.sector['period_end']) &  # filter samples with consensus prediction
                                       (self.sector['period_end'] < testing_period)][['eps1fd12','fn_8001','fn_18263']]
        train_id = self.sector.loc[(start_train_y <= self.sector['period_end']) &  # training sets id
                                       (self.sector['period_end'] < testing_period)]['identifier'].to_list()
        test_y = self.sector.loc[self.sector['period_end'] == testing_period]['y_{}'.format(y_type)]   # filter samples with consensus prediction
        test_filter = self.sector.loc[self.sector['period_end'] == testing_period][['eps1fd12','fn_8001','fn_18263']]
        test_id = self.sector.loc[self.sector['period_end'] == testing_period]['identifier'].to_list() # testing sets id

        train_y, test_y = self.y_qcut(train_y, test_y, qcut_q)  # qcut & convert to median for training / testing

        # 2. split and standardize train / test X
        x_col = list(set(self.sector.columns.to_list()) - {'identifier', 'period_end', 'icb_sector', 'market',
                                                           'icb_industry', 'y_ni', 'y_ibes', 'y_rev', 'eps_rnn'})    # define x_fields
        if eps_only == True:
            x_col = {'eps_rnn'}
        elif exclude_fwd == True:
            x_col = list(set(x_col) - {'eps1tr12','ebd1fd12', 'cap1fd12', 'eps1fd12'})

        # 2.1. slice data for sample period + lookback period
        start_train = testing_period - relativedelta(years=15)    # train df = 10y + 5y lookback
        start_test = testing_period - relativedelta(years=5)      # test df = 1q * 5y lookback

        train_2dx_info = self.sector.loc[(start_train <= self.sector['period_end']) & (self.sector['period_end'] < testing_period)] # extract df for X
        test_2dx_info = self.sector.loc[(start_test <= self.sector['period_end']) & (self.sector['period_end'] <= testing_period)]
        # print(len(test_2dx_info), idd in test_2dx_info['identifier'].to_list())

        # 2.2. standardize data
        train_2dx_info.loc[:, x_col], test_2dx_info.loc[:, x_col] = self.standardize_x(train_2dx_info[x_col], test_2dx_info[x_col])  # standardize x

        # 2.3. convert 2D -> 3D data (add lookback axis)
        def to_3d(train_2dx_info, period_range):
            ''' convert 2d DF -> 3d array'''

            df = train_2dx_info.fillna(0)       # fill nan with 0
            train_3dx_all = df.set_index(['period_end', 'identifier'])[x_col].to_xarray().to_array().transpose() # training (batchsize, 60, x_fields)

            arr = []
            id = []
            for i in period_range: # slice every 20q data as sample & reduce lookback (60 -> 20) (axis=1)
                arr.append(train_3dx_all[:,(1+i):(21+i),:].values)
                id.append(np.array(train_3dx_all.identifier))

            return np.concatenate(arr, axis=0)  # concat sliced samples & increase batchsize (axis=0)

        train_x = to_3d(train_2dx_info, range(40))  # convert to 3d array
        test_x = to_3d(test_2dx_info, [0])

        # 2.4. remove samples without Y
        if small_training == True: # using samples with consensus prediction
            train_mask = np.logical_or(np.isnan(train_y[:, 0]), np.isnan(train_filter.values).any(axis=1)) # y_ibes / eps1fd12 is not np.nan
            test_mask = np.logical_or(np.isnan(test_y[:, 0]), np.isnan(test_filter.values).any(axis=1))
        else:
            train_mask = np.isnan(train_y[:, 0])
            test_mask = np.isnan(test_y[:, 0])

        train_x = train_x[~train_mask]  # remove y = nan
        train_id = np.array(train_id)[~train_mask]
        train_y = train_y[~train_mask]

        test_x = test_x[~test_mask]
        test_id = np.array(test_id)[~test_mask]    # records identifier for testing set for TABLE results_rnn_stock
        test_y = test_y[~test_mask]

        # 3. split 5-Fold cross validation testing set -> 5 tuple contain lists for Training / Validation set
        cv = GroupShuffleSplit(n_splits=5).split(train_x, train_y, groups = train_id)

        return train_x, train_y, test_x, test_y, cv, test_id, x_col

    def standardize_x(self, train_x, test_x):
        ''' tandardize x with train_x fit '''

        scaler = StandardScaler().fit(train_x)
        train_x = scaler.transform(train_x)
        test_x = scaler.transform(test_x)

        return train_x, test_x

    def y_qcut(self, train_y, test_y, qcut_q):
        ''' qcut y '''

        ''' convert qcut bins to median of each group '''

        self.cut_bins = {}

        # cut original series into 0, 1, .... (bins * n)
        train_y_qcut, self.cut_bins['cut_bins'] = pd.qcut(train_y, q=qcut_q, retbins=True, labels=False)
        test_y_qcut = pd.cut(test_y, bins=self.cut_bins['cut_bins'], labels=False)

        # calculate median on train_y for each qcut group
        df = pd.DataFrame(np.vstack((train_y, np.array(train_y_qcut)))).T   # concat original series / qcut series
        self.cut_bins['med_train'] = df.groupby([1]).median().sort_index()[0].to_list()     # find median of each group

        # replace 0, 1, ... into median
        train_y = pd.DataFrame(train_y_qcut).replace(range(qcut_q), self.cut_bins['med_train']).values
        test_y = pd.DataFrame(test_y_qcut).replace(range(qcut_q), self.cut_bins['med_train']).values

        return train_y, test_y


if __name__ == '__main__':

    add_ind_code = 0
    period_1 = dt.datetime(2015, 12, 31)
    sample_no = 25
    load_data_params = {'qcut_q': 10, 'y_type': 'ibes', 'exclude_fwd': False, 'eps_only': True}

    data = load_data(macro_monthly=True)
    data.split_entire(add_ind_code)

    for i in tqdm(range(sample_no)):  # roll over testing period
        testing_period = period_1 + i * relativedelta(months=3) - relativedelta(days=1)
        print(testing_period)

        train_x, train_y, X_test, Y_test, cv, test_id, x_col = data.split_train_test(testing_period, **load_data_params)
        print(X_test.shape)
        continue

        for train_index, test_index in cv:

            X_train = train_x[train_index]
            Y_train = train_y[train_index]
            X_valid = train_x[test_index]
            Y_valid = train_y[test_index]

            print(X_train.shape, Y_train.shape, X_valid.shape, Y_valid.shape, X_test.shape, Y_test.shape)
            continue

