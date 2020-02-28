# coding: utf-8
# Author: 阿财（Rgveda@github）（11652964@qq.com）
# Created date: 2018-06-08
#
# The MIT License (MIT)
#
# Copyright (c) 2016-2018 yutiansut/QUANTAXIS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
火币api
具体api文档参考: https://huobiapi.github.io/docs/spot/v1/cn/
"""
import requests
import gzip
import json
import datetime
import time
from dateutil.relativedelta import relativedelta
import pandas as pd
import numpy as np
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
from requests.exceptions import ConnectTimeout, SSLError, ReadTimeout, ConnectionError
from retrying import retry

from urllib.parse import urljoin

from QUANTAXIS.QAUtil.QADate_Adv import (
    QA_util_str_to_Unix_timestamp,
    QA_util_datetime_to_Unix_timestamp,
    QA_util_timestamp_to_str
)
from QUANTAXIS.QAUtil.QAcrypto import QA_util_find_missing_kline
from QUANTAXIS.QAUtil.QALogs import (
    QA_util_log_info,
    QA_util_log_expection,
    QA_util_log_debug
)
from QUANTAXIS.QAFetch.QAhuobi_realtime import (
    QA_Fetch_Job_Status,
    QA_Fetch_Job_Type,
    QA_Fetch_Job,
    QA_Fetch_Huobi
)


# from QUANTAXIS.QAUtil.QAcrypto import TIMEOUT, ILOVECHINA
TIMEOUT = 10
ILOVECHINA = "同学！！你知道什么叫做科学上网么？ 如果你不知道的话，那么就加油吧！蓝灯，喵帕斯，VPS，阴阳师，v2ray，随便什么来一个！我翻墙我骄傲！"
Huobi_base_url = 'https://api.huobi.pro/'


class CandlestickInterval:
    MIN1 = "1min"
    MIN5 = "5min"
    MIN15 = "15min"
    MIN30 = "30min"
    MIN60 = "60min"
    HOUR4 = "4hour"
    DAY1 = "1day"
    MON1 = "1mon"
    WEEK1 = "1week"
    YEAR1 = "1year"
    INVALID = None

"""
QUANTAXIS 和 Huobi.pro 的 frequency 常量映射关系
"""
Huobi2QA_FREQUENCY_DICT = {
    CandlestickInterval.MIN1: '1min',
    CandlestickInterval.MIN5: '5min',
    CandlestickInterval.MIN15: '15min',
    CandlestickInterval.MIN30: '30min',
    CandlestickInterval.MIN60: '60min',
    CandlestickInterval.DAY1: '1day'
}
"""
Huobi 只允许一次获取 300bar，时间请求超过范围则不返回数据
"""
FREQUENCY_SHIFTING = {
    CandlestickInterval.MIN1: 14400,
    CandlestickInterval.MIN5: 72000,
    CandlestickInterval.MIN15: 216000,
    CandlestickInterval.MIN30: 432000,
    CandlestickInterval.MIN60: 864000,
    CandlestickInterval.DAY1: 20736000
}

FIRST_PRIORITY = [
    'atomusdt',
    'algousdt',
    'adausdt',
    'bchusdt',
    'bsvusdt',
    'btcusdt',
    'btchusd',
    'dashusdt',
    'dashhusd',
    'eoshusd',
    'eosusdt',
    'etcusdt',
    'etchusd',
    'ethhusd',
    'ethusdt',
    'hthusd',
    'htusdt',
    'hb10usdt',
    'ltcusdt',
    'trxusdt',
    'xmrusdt',
    'xrpusdt',
    'zecusdt'
]

def format_huobi_data_fields(datas, symbol, frequency):
    """
    # 归一化数据字段，转换填充必须字段，删除多余字段
    参数名 	类型 	描述
    id 	    int32 	开始时间
    open 	String 	开盘价格
    high 	String 	最高价格
    low 	String 	最低价格
    close 	String 	收盘价格
    vol 	float 	交易量
    amount  float   交易量（本币）
    """
    # 归一化数据字段，转换填充必须字段，删除多余字段
    frame = pd.DataFrame(datas)
    frame['symbol'] = symbol
    frame['market'] = 'huobi'
    # UTC时间转换为北京时间
    frame['date'] = pd.to_datetime(
        frame['id'],
        unit='s'
    ).dt.tz_localize('UTC').dt.tz_convert('Asia/Shanghai')
    frame['date'] = frame['date'].dt.strftime('%Y-%m-%d')
    frame['datetime'] = pd.to_datetime(
        frame['id'],
        unit='s'
    ).dt.tz_localize('UTC').dt.tz_convert('Asia/Shanghai')
    frame['datetime'] = frame['datetime'].dt.strftime('%Y-%m-%d %H:%M:%S')
    # 北京时间转换为 UTC Timestamp
    frame['date_stamp'] = pd.to_datetime(
        frame['date']
    ).dt.tz_localize('Asia/Shanghai').astype(np.int64) // 10**9
    frame['created_at'] = time.mktime(datetime.datetime.now().utctimetuple())
    frame['updated_at'] = time.mktime(datetime.datetime.now().utctimetuple())
    frame.rename({'count': 'trade', 'id': 'time_stamp', 'vol': 'volume'}, axis=1, inplace=True)
    if (frequency not in [CandlestickInterval.DAY1, Huobi2QA_FREQUENCY_DICT[CandlestickInterval.DAY1], '1d']):
        frame['type'] = Huobi2QA_FREQUENCY_DICT[frequency]
    return frame


@retry(stop_max_attempt_number=3, wait_random_min=50, wait_random_max=100)
def QA_fetch_huobi_symbols():
    """
    Get Symbol and currencies
    """
    url = urljoin(Huobi_base_url, "/v1/common/symbols")
    retries = 1
    datas = list()
    while (retries != 0):
        try:
            req = requests.get(url, timeout=TIMEOUT)
            retries = 0
        except (ConnectTimeout, ConnectionError, SSLError, ReadTimeout):
            retries = retries + 1
            if (retries % 6 == 0):
                print(ILOVECHINA)
            print("Retry get_exchange_info #{}".format(retries - 1))
            time.sleep(0.5)
    if (retries == 0):
        msg_dict = json.loads(req.content)
        if (('status' in msg_dict) and (msg_dict['status'] == 'ok')
                and ('data' in msg_dict)):
            if len(msg_dict["data"]) == 0:
                return []
            for symbol in msg_dict["data"]:
                # 只导入上架交易对
                if (symbol['state'] == 'online'):
                    datas.append(symbol)

    return datas


@retry(stop_max_attempt_number=3, wait_random_min=50, wait_random_max=100)
def QA_fetch_huobi_kline(
    symbol,
    start_time,
    end_time,
    frequency,
    callback_save_data_func
):
    """
    Get the latest symbol‘s candlestick data  
    当前 REST API 不支持自定义时间区间，如需要历史固定时间范围的数据，请参考 Websocket API 中的 K 线接口。
    """
    datas = list()
    retries = 1
    url = urljoin(
        Huobi_base_url,
        "/market/history/kline?symbol={:s}&period={:s}&siz={:d}".format(
            symbol,
            frequency,
            2000
        )
    )
    while (retries != 0):
        try:
            req = requests.get(url, timeout=TIMEOUT)
            # 防止频率过快被断连
            time.sleep(0.5)
            retries = 0
        except (ConnectTimeout, ConnectionError, SSLError, ReadTimeout):
            retries = retries + 1
            if (retries % 6 == 0):
                print(ILOVECHINA)
            print("Retry get_candlestick #{}".format(retries - 1))
            time.sleep(0.5)
        except HuobiApiException as e:
            print(e.error_code)
            print(e.error_message)
            print("Skipping '{}'".format(symbol))
            time.sleep(0.5)
            break

    if (retries == 0):
        # 成功获取才处理数据，否则继续尝试连接
        msg_dict = json.loads(req.content)
        if (('status' in msg_dict) and (msg_dict['status'] == 'ok')
                and ('data' in msg_dict)):
            if len(msg_dict["data"]) == 0:
                return None
            for kline in msg_dict["data"]:
                datas.append(kline)
            # 狗日huobi.pro的REST API kline时间戳排序居然是倒序向前获取，必须从后向前获取，而且有数量限制，
            # Request < 2000,
            if (callback_save_data_func is not None):
                frame = format_huobi_data_fields(datas, symbol, frequency)
                callback_save_data_func(frame, freq=Huobi2QA_FREQUENCY_DICT[frequency])

    if len(datas) == 0:
        return None

    # 归一化数据字段，转换填充必须字段，删除多余字段
    frame = format_huobi_data_fields(datas, symbol, frequency)
    return frame


@retry(stop_max_attempt_number=3, wait_random_min=50, wait_random_max=100)
def QA_fetch_huobi_kline_subscription(
    symbol,
    start_time,
    end_time,
    frequency,
    callback_save_data_func
):
    """
    Get the symbol‘s candlestick data by subscription
    """
    reqParams = {}
    reqParams['from'] = int(end_time - FREQUENCY_SHIFTING[frequency])
    reqParams['to'] = int(end_time)

    requested_counter = 1
    sub_client = QA_Fetch_Huobi(
        callback_save_data_func=callback_save_data_func,
        find_missing_kline_func=QA_util_find_missing_kline
    )
    datas = list()
    while (reqParams['to'] > start_time):
        if ((reqParams['from'] > QA_util_datetime_to_Unix_timestamp())) or \
            ((reqParams['from'] > reqParams['to'])):
            # 出现“未来”时间，一般是默认时区设置，或者时间窗口滚动前移错误造成的
            raise Exception(
                'A unexpected \'Future\' timestamp got, Please check self.missing_data_list_func param \'tzlocalize\' set. More info: {:s}@{:s} at {:s} but current time is {}'
                .format(
                    symbol,
                    frequency,
                    QA_util_print_timestamp(reqParams['to']),
                    QA_util_print_timestamp(
                        QA_util_datetime_to_Unix_timestamp()
                    )
                )
            )

        retries = 1
        frame = None
        while (retries != 0):
            try:
                frame = sub_client.run_request_historical_kline(
                    symbol,
                    frequency,
                    reqParams['from'],
                    reqParams['to'],
                    requested_counter
                )
                if (frame is None):
                    # 火币网的 WebSocket 接口机制很奇特，返回len(data)==0
                    # 就说明已经超越这个交易对的上架时间，不再有更多数据了。
                    # 当前 Symbol Klines 抓取已经结束了
                    return None
                else:
                    retries = 0
            except Exception:
                retries = retries + 1
                if (retries % 6 == 0):
                    print(ILOVECHINA)
                print("Retry request_historical_kline #{}".format(retries - 1))
                time.sleep(0.5)
            if (retries == 0):
                # 等待3秒，请求下一个时间段的批量K线数据
                reqParams['to'] = int(reqParams['from'] - 1)
                reqParams['from'
                         ] = int(reqParams['from'] - FREQUENCY_SHIFTING[frequency])
                requested_counter = requested_counter + 1
                # 这一步冗余，如果是开启实时抓取会自动被 WebSocket.on_messgae_callback 事件处理函数保存，
                # 但不确定不开启实时行情抓取会不会绑定on_messgae事件，所以保留冗余。
                callback_save_data_func(data=frame, freq=frequency)
        time.sleep(0.5)
    
    if (frame is None):
        return None

    return frame


if __name__ == '__main__':
    #from dateutil.tz import tzutc
    #from QUANTAXIS.QASU.save_huobi import QA_SU_save_data_huobi_callback
    pass
