from __future__ import annotations
from typing import Iterable, Type, Union
from parser import ParserError
from prometheus_client import Summary, Counter, Gauge, Enum
import datetime as dt
import re

import numpy as np
import pandas as pd

# For some metrics, we may want to keep a FIFO que for
# calculating summary statistics over fixed number of records
# instead of a period of time that is the standard for Prometheus:

class FifoOverwriteDataFrame:
    """
    A FIFO Queue for storing maxsize latest items.
    
    Properties:
     - if full, will replace the oldest value with the newest
     - can only be emptied if completely full (flush)
     - optionally clear (drop all) at flush
    """
    def __init__(self, columns: dict, maxsize: int = 1000, clear_at_flush: bool = False):
        self.is_full = False
        self.maxsize = maxsize
        self.columns = columns
        self.clear_at_flush = clear_at_flush
        self.df = pd.DataFrame(columns=columns)

    def put(self, rows: Union[np.ndarray, Iterable, dict, pd.DataFrame])->FifoOverwriteDataFrame:
        """
        Put new items to queue. If full, overwrite the oldest value.
        Return reference to self.
        """
        y_size = self.df.shape[0]
        new_data = pd.DataFrame(rows, columns=self.columns)
        if new_data.shape[0] >= self.maxsize:
            new_data = new_data.iloc[-self.maxsize:]
        self.df = pd.concat(
            (self.df.iloc[1:] if y_size == self.maxsize else self.df,
                new_data),
            ignore_index=True)
        if self.df.shape[0] == self.maxsize:
            self.is_full = True
        else:
            self.is_full = False

        return self

    def flush(self)->pd.DataFrame:
        """
        If queue df is full, return copy.
        If self.clear_at_flush, clear df before returning copy. Queue is only cleared if full.
        Else, return false.
        """
        if not self.is_full: return False
        ret = self.df.copy()
        if self.clear_at_flush: self.df.drop(self.df.index, inplace=True)
        return ret
# Prometheus naming conventions:
#
# suffix describing base unit, plural form
# _total for unitless count
# _unit_total for unit accumulating count
# _info for metadata
# _timestamp_seconds for timestamps
# rule of thumb: either sum() or avg() of metric must be meaningful, or metric has to be split up
# 
# commonly used: (elsewhere -> in prometheus)
# time -> seconds
# percent -> ratio  0-1
# bits, bytes -> bytes


def convert_time_to_seconds(t, errors: str = 'raise', pd_infer_datetime_format: bool = True, pd_str_parse_format: bool = None):
    """
    Prometheus standard is to express all time in seconds.
    This function parses time expressions to seconds in accuracy of floats.

    Recursive function, parses:
        strings -> pandas -> numpy -> float
        datetime -> float
        int -> float
    
    Parameters:

        errors: {‘ignore’, ‘raise’, ‘coerce’}, default ‘raise’
            - If 'raise', raise an exception.
            - If 'coerce', return np.nan.
            - If 'ignore', return the input.

        pd_infer_datetime_format: bool, default True
            
        pd_str_parse_format: str, default none

    By default try to infer format from string inputs. This is slower compared to parsing defined format.
    Overwrite by passing custom format to parameter pd_str_parse_format.
    Force custom format by setting pd_infer_datetime_format to False.
    """
    
    if errors not in ['raise', 'ignore', 'coerce']:
        raise ValueError(f'{errors} is not a valid argument for parameter errors!')

    try:
        # strings
        if isinstance(t, (str, pd.StringDtype)):
            try:
                try:
                    ret = pd.to_datetime(t, infer_datetime_format=pd_infer_datetime_format,
                        format = pd_str_parse_format)
                except (pd.errors.ParserError, ValueError):
                    try:
                        ret = pd.to_timedelta(t)
                    except pd.errors.ParserError:
                        ret = pd.Period(t)
            except ValueError:
                raise ValueError(f'Unsupported expression of time: {t}' + \
                    '\nDo you have a metric with name *time* or *date* that is not an expression of time?')
            return convert_time_to_seconds(ret)
        
        # pandas
        elif isinstance(t, (pd.Timestamp, pd.Timedelta)):
            return convert_time_to_seconds(t.to_numpy())
        elif isinstance(t, pd.Period):
            return convert_time_to_seconds(t.to_timestamp(how='E')-t.to_timestamp(how='S'))

        # numpy
        elif isinstance(t, np.datetime64):
            return convert_time_to_seconds(t-np.datetime64('1970-01-01'))
        elif isinstance(t, np.timedelta64):
            return t/np.timedelta64(1, 's')

        # datetime
        elif isinstance(t,dt.date):
            return float(((t.year*12 + t.month)*31 + t.day)*24*60*60)
        elif isinstance(t, dt.time):
            return float((t.hour*60+ t.minute)*60 + t.second)
        elif isinstance(t, dt.datetime):
            return convert_time_to_seconds((t-dt.datetime.min).timestamp())
        elif isinstance(t, dt.timedelta):
            return t/dt.timedelta(seconds=1)
        # other (int, float)
        else:
            return float(t)
    except Exception as e:
        if errors == 'raise':
            raise e
        elif errors == 'coerce':
            return np.nan
        else:
            return t

def create_promql_metric_name(metric_name: str,
                                dtype: Type,
                                prefix: str = '',
                                suffix: str = '',
                                is_counter = False):
    """
    Create a promql compatible name for a metric.
    Note that this does not perfectly ensure good naming,
    but may help when auto-generating metrics.

    See https://prometheus.io/docs/practices/naming/ for naming conventions.
    """

    ret = prefix.rstrip('_') # may not begin with underscore
    if isinstance(dtype(),(datetime, pd.Timestamp, np.datetime64)):
        # e.g. date_of_birth -> 'date_of_birth_timestamp_seconds'
        metric_name =  metric_name.strip('_seconds').strip('_timestamp').strip('_seconds') + \
            '_timestamp_seconds'
    elif isinstance(dtype(),(pd.Timedelta, pd.Period,np.timedelta64)):
        # e.g. time_on_site -> 'time_on_site_seconds'
        metric_name = metric_name.strip('_seconds') + '_seconds'
    elif isinstance(dtype(), (str, np.string_, np.unicode_, np.dtype('O'),
                        pd.object, pd.CategoricalDtype, pd.StringDtype)):
        # e.g. description -> description_count
        metric_name += '_info'
    else: # add more exceptions if needed
        pass
    
    ret += metric_name + '_' + suffix

    # remove metric types from metric name (a metric name should not contain these)
    for metric_type in ['gauge', 'counter', 'summary', 'map']:
        metric_name = metric_name.replace(metric_type, '')
    
    # remove reserved suffixes (a metric should not end with these)
    for metric_suffix in ['_count', '_sum', '_bucket', '_total']:
        metric_name = metric_name.strip(metric_suffix)

    # however, counters should always have _total suffix
    if is_counter: metric_name += '_total'

    # clean extra underscores
    metric_name = ''.join(['_' + val.strip('_') if val != '' else '' for val in metric_name.split('_')]).rstrip('_')
    
    # clean non-alphanumericals and underscores
    metric_name = re.sub('[a-zA-Z_:][a-zA-Z0-9_:]*', '_', s)

    return metric_name


def model_development_metrics(metrics: dict) -> None:
    """
    Pass pre-recorded metrics from dict to Prometheus.

    This allows recording three types of metrics
        - numeric (int, float, etc.)
        - categorical
        - info (metadata)
    Lists and matrices must be split so that each cell is their own metric.

    For designing metrics, see Prometheus naming conventions see:
        https://prometheus.io/docs/practices/naming/
    For metadata (model version, data version etc. see:
        https://www.robustperception.io/exposing-the-software-version-to-prometheus/

    Format:
    metrics = {
        'metric_name':{
            'value': int, float or str if 'type' = category,
            'description': str,
            'type': str -> 'numeric', 'category' or 'info' (metadata / pseudo metrics),
            'categories': [str], e.g. ['A', 'B', 'C']. only required if 'type' = category,
            'label_names': [str], optional. for info type metrics
            'label_values': [str], optional. for info type metrics
        }
    }

    Example: 

    metrics = {
        'train_loss':{'value':0.95, 'description': 'training loss (MSE)', 'type':'numeric'},
        'test_loss':{'value':0.96, 'description': 'test loss (MSE)', 'type':'numeric'},
        'optimizer':{'value':random.choice(['SGM', 'RMSProp', 'Adagrad']),
            'description':'ml model optimizer function',
            'type': 'category',
            'categories':['SGD', 'RMSProp', 'Adagrad', 'Adam']}
        'model_build_info':{'description': 'dev information', type: 'info',
            label_names = ['origin', 'branch', 'commit'],
            label_values = ['city-of-helsinki@github.com/ml-app', 'main', '12354568']}
            }
    """
    # TODO: make use of create_promql_metric_name

    for metric_name in metrics.keys():
        metric = metrics[metric_name]
        if metric['type'] == 'numeric':
            g = Gauge(metric_name, metric['description'])
            g.set(metric['value'])
        elif metric['type'] == 'category':
            s = Enum(
                metric_name,
                metric['description'],
                states = metric['categories']
            )
            s.state(metric['value'])
        elif metric['type'] == 'info':
            g = Gauge(metric_name.strip('_info') + '_info', metric['description'], 
                metric['label_names'])
            # note that each new metric-label combination creates a new time series!
            g.labels(metric['label_values']).set(1)
        else:
            raise ValueError(f'metric of unknown type: {metric}')

# TODO: PROMEHEUS:
# input:
#   - raw values (if not text or some other weird datatype)
#   - hist/sumstat (a bit more private)



class SummaryStatisticsMetrics:
    """
    Class for wrapping metrics based on dataframe summary statistics, 
    for example FifoOverwriteDataFrame.flush()
    """

    def init(self,
            columns: dict,
            summary_statistics_function: function = lambda df: df.describe(include = 'all'),
            metrics_name_prefix: str = ''):
        """
        metric_name_prefix is a common prefix for metric names, e.g. 'input_feature_'
        """
        self.columns = columns
        self.summary_statistics_function = summary_statistics_function
        
        # initialize metric names by calling summary statistics on an empty dataframe
        self.rownames = summary_statistics_function(pd.DataFrame(columns=dict)).index.values
        self.colnames =list(self.columns.keys())

        self.metrics = {} # store metric handles in a dict
        for colname in self.colnames:
            dtype = columns['colname']['dtype']
            for rowname in self.rownames:
                metric_key = '_'.join([colname, rowname])
                metric_description = f'calculated using summary statistics function {summary_statistics_function.__name__}'
                metric_name = create_promql_metric_name(metric_name=colname,
                    dtype=dtype,
                    prefix = metrics_name_prefix,
                    postfix = rowname
                )
                # if category, create Enum
                if isinstance(dtype(), (str, np.string_, np.unicode_, np.dtype('O'),
                        pd.object, pd.CategoricalDtype, pd.StringDtype)):
                    g = Enum(
                        metric_name,
                        metric_description,
                        states = []
                    )
                # else Gauge
                else:
                    g = Gauge(metric_name, metric['description'])
                self.metrics[metric_key] = g

    def set(self, df: pd.DataFrame):
        """
        Set metrics to given value
        """
        sumstat_df = self.summary_statistics_function(df)
        # loop through metrics and 
        for colname in self.colnames:
            for rowname in self.rownames:
                metric_key = '_'.join([colname, rowname])
                metric_value = sumstat_df.loc[rowname, colname]
                if isinstance(metric_value,(datetime, pd.Timestamp,
                        pd.Timedelta, pd.Period, np.datetime64, np.timedelta64)):
                    # convert all time formats to integer seconds
                    metric_value = convert_time_to_seconds(feature, dtype)
                elif isinstance(metric_value, (bool, np.bool_, pd.BooleanDtype)):
                    metric_value = 1 if metric_value else 0
                elif isinstance(metric_value, (str, np.string_, np.unicode_, np.dtype('O'),
                        pd.object, pd.CategoricalDtype, pd.StringDtype)):
                    metric_value = str(metric_value)
                else:
                    pass # add other options if needed
                # omit nan values
                if metric_value is not None and not np.isnan(metric_value):
                    self.metrics[metric_key].set(metric_value)

        

    

# processing:
#   - time (total / hist )
#   - general resource usage
#   - request counter
# output:
#   - raw (if not text of some other weird datatype)
#   - if category
#   - hist/sumstat (a bit more private)
#   - live_scoring



