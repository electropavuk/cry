import json
from itertools import product, accumulate
from copy import deepcopy
import time
import heapq
from pprint import pprint
import multiprocessing as mp

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

import rules
import indicators
import trader
import experts
import data
import config



class Trainer:
    rule_names = [
        'MovingAverageCrossoverRule',
        'ExponentialMovingAverageCrossoverRule',
        'RelativeStrengthIndexTrasholdRule',
        'TripleExponentialDirectionChangeRule',
        'IchimokuKinkoHyoTenkanKijunCrossoverRule',
        'IchimokuKinkoHyoSenkouASenkouBCrossoverRule',
        'IchimokuKinkoHyoChikouCrossoverRule',
        # 'IchimokuKinkoHyoSenkouASenkouBSupportResistanceRule',
        'BollingerBandsLowerUpperCrossoverRule',
        'BollingerBandsLowerMidCrossoverRule',
        'BollingerBandsUpperMidCrossoverRule',
        'MovingAverageConvergenceDivergenceSignalLineCrossoverRule',
    ]

    timeframes = ['1d', '4h', '1h', '15m', '1m']

    def __init__(self):
        self.loaded_history = {}

    def construct_system(self):
        timeframes = self.timeframes

        reestimate = False
        reestimate = True

        if reestimate:
            config.create_searchspace_config()

            pair = 'BTC/USDT'
            base, quote = pair.split('/')
            timeframe_lst = []
            for timeframe in timeframes:
                rule_cls_lst = []
                print(f'load timeframe [{timeframe}]')

                for rule in self.rule_names:
                    new = [expert for expert in config.get_experts_from_searchspace(timeframe, rule)]
                    print(' ' * 5 + f'{rule:<60} {len(new):>5} candidates')
                    rule_cls_expert = experts.RuleClassExpert(rule)
                    rule_cls_expert.set_experts(new)
                    rule_cls_lst.append(rule_cls_expert)

                timeframe_expert = experts.TimeFrameExpert(timeframe)
                timeframe_expert.set_experts(rule_cls_lst)
                timeframe_lst.append(timeframe_expert)

            pair_expert = experts.PairExpert(base, quote)
            pair_expert.set_experts(timeframe_lst)
            self.trim_bad_experts(pair_expert, nbest=99999)
            pair_expert.show()
            config.serialize_expert_to_json(filename='estimated_expert.json', expert=pair_expert)

        else:
            pair_expert = config.deserialize_expert_from_json('estimated_expert.json')

        self.trim_bad_experts(pair_expert, min_trades=10, trashold=.15)
        config.serialize_expert_to_json(expert=pair_expert)

    def trim_bad_experts(self, expert: experts.BaseExpert, *, indentation: int = 0, **kwargs):
        print(f'{" " * indentation}trim {expert.name}')
        if isinstance(expert, experts.RuleClassExpert):
            expert._inner_experts = self.best_rule_experts(expert._inner_experts, rule=expert.rule, **kwargs)
        else:
            kwargs |= {attr: getattr(expert, attr) for attr in ('base', 'quote', 'pair', 'timeframe') if hasattr(expert, attr)}
            for expert in expert._inner_experts:
                self.trim_bad_experts(expert, indentation=indentation+10, **kwargs)

    def best_rule_experts(self, candidates: list[experts.RuleExpert],
                                min_trades: int = None, *,
                                trashold: float = None,
                                nbest: int = None,
                                percent: 'float (0, 1)' = None,
                                **kwargs) -> list[experts.RuleExpert]:
        nbest = nbest if percent is None else int(percent * len(candidates))
        for expert in candidates:
            self.estimate_expert(expert, **kwargs)
        if min_trades is not None:
            candidates = [expert for expert in candidates if expert._estimated_ntrades >= min_trades]
        candidates.sort(reverse=True, key=lambda x: x._estimated_profit)
        if trashold is not None:
            return [expert for expert in candidates if expert._estimated_profit > trashold]
        elif nbest is not None:
            return candidates[:nbest]

    def estimate_expert(self, expert: experts.RuleExpert,
                              pair: str,
                              timeframe: str,
                              **kwargs):
        ndays = {'1d': 180, '4h': 180, '1h': 90, '15m': 30, '1m': 3}
        if expert._estimated_profit is None:
            pair_trader = trader.PairTrader(pair)
            pair_expert = self.cast_to_pair_expert(expert, timeframe=timeframe, **kwargs)
            pair_expert.set_weights(recursive=True)
            pair_trader.set_expert(pair_expert)
            profit, ntrades = self.simulate_pair_trader(pair_trader, ndays=ndays[timeframe])
            expert._estimated_profit = profit / ndays[timeframe]
            expert._estimated_ntrades = ntrades

    def cast_to_pair_expert(self, expert: experts.BaseExpert,
                                  quote: str,
                                  base: str,
                                  timeframe: str = None,
                                  rule: str = None) -> experts.PairExpert:
        for rule_cls, upcast_cls, args in zip([experts.RuleExpert, experts.RuleClassExpert, experts.TimeFrameExpert],
                                              [experts.RuleClassExpert, experts.TimeFrameExpert, experts.PairExpert],
                                              [(rule,), (timeframe,), (quote, base)]):
            if isinstance(expert, rule_cls):
                assert None not in args, f'No arguments for {upcast_cls}'
                temp = upcast_cls(*args)
                temp.set_experts([expert])
                expert = temp
                expert.set_weights()
        return expert

    def simulate_pair_trader(self, pair_trader: trader.PairTrader, ndays: int, *, display: bool = False):
        def load_history(pair: str, timeframe: str) -> pd.DataFrame:
            if (pair, timeframe) not in self.loaded_history:
                filename = f"data/test_data/{pair.replace('/', '')}/{timeframe}.csv"
                self.loaded_history[(pair, timeframe)] = pd.read_csv(filename)
            return self.loaded_history[(pair, timeframe)]

        def construct_data(pair_trader: trader.PairTrader, ndays: int):
            init_data = data.DataMaintainer()
            new_data = {}
            start_time = load_history(pair_trader.pair, '1d')['Close time'].iloc[-ndays]
            for timeframe in pair_trader.timeframes:
                df = load_history(pair_trader.pair, timeframe)
                split = df['Close time'].searchsorted(start_time)
                init, new = df.iloc[max(split-1000, 0): split].values.T, df.iloc[split:].values
                mapping = {key: val for key, val in zip(df, init)}
                init_data.add(mapping, location=[timeframe, 'Init'])
                new_data[timeframe] = new
            return init_data, new_data

        init_data, new_data = construct_data(pair_trader, ndays + 1)
        pair_trader.set_data(init_data)

        new_data_iter = {timeframe: iter(data) for timeframe, data in new_data.items()}
        minutes = {timeframe: n for timeframe, n in zip(['1m', '15m', '1h', '4h', '1d'], [1, 15, 60, 240, 1440])}
        simulation_length = minutes['1d'] * ndays

        for i in range(0, simulation_length, minutes[pair_trader.min_timeframe]):
            update = {}
            for timeframe in pair_trader.timeframes:
                if not i % minutes[timeframe]:
                    update[timeframe] = next(new_data_iter[timeframe])
            pair_trader.update(update)
            pair_trader.act()

        if display:
            self.show_trades(pair_trader, new_data)
        return pair_trader.evaluate_profit(), len(pair_trader.trades)

    def fit_weights(self, expert: experts.BaseExpert, pair='BTC/USDT', epochs=15, population=10, nchildren=3, indentation=0, **kwargs):
        def estimate_trader(pair_trader: trader.PairTrader, *, ret_dict = None) -> float:
            profit, ntrades = self.simulate_pair_trader(pair_trader, ndays=90)
            ret_dict[hash(pair_trader)] = profit if ntrades >= min_trades else -999

        def change_weights(weights: np.array):
            sigma = lr * np.exp(-decay * epoch)
            return weights + np.random.normal(size=weights.shape, scale=sigma)

        def parallel_estimation(traders: list[trader.PairTrader]):
            results = mp.Manager().dict()
            jobs = [mp.Process(target=estimate_trader, args=(trader,), kwargs={'ret_dict': results}) for trader in traders]
            for job in jobs:
                job.start()
            for job in jobs:
                job.join()
            return [results[hash(trader)] for trader in traders]

        kwargs |= {attr: getattr(expert, attr) for attr in ('quote', 'base', 'timeframe', 'rule') if hasattr(expert, attr)}
        if not isinstance(expert, experts.RuleClassExpert):
            for exp in expert._inner_experts:
                self.fit_weights(exp, indentation=indentation+10, **kwargs)

        if len(expert._inner_experts) > 1:
            lr, decay = 1, .2
            min_trades = 10
            parents = [expert.get_weights()]

            print(' ' * indentation + f'{expert.name}')
            for epoch in range(epochs):
                children = []
                for weights in parents:
                    children += [change_weights(weights) for _ in range(nchildren)]
                parents += children

                traders = []
                for weights in parents:
                    exp = deepcopy(expert)
                    exp.set_weights(weights)
                    exp = self.cast_to_pair_expert(exp, **kwargs)
                    tr = trader.PairTrader(pair)
                    tr.set_expert(exp)
                    traders.append(tr)

                estimations = parallel_estimation(traders)

                results = zip(estimations, parents)
                results = heapq.nlargest(population, results, key=lambda x: x[0])
                parents = [weights for profit, weights in results]

                best_profit, best_weights = max(results, key=lambda x: x[0])
                if expert._estimated_profit is None or expert._estimated_profit < best_profit:
                    expert._estimated_profit = best_profit
                    expert.set_weights(best_weights)

                print(' ' * (indentation + 100) + f'[ {epoch+1:>3} / {epochs:<3} ] profit: {best_profit:.2f} %')

    def show_trades(self, pair_trader: trader.PairTrader, new_data: dict):
        def config_axs(*axs):
            for ax in axs:
                ax.grid(True)
                ax.set_xlim(time[0], time[-1])
                ax.margins(x=.1)

        pair_trader.show_evaluation()

        timeframe = pair_trader.min_timeframe
        close = new_data[timeframe][:, 4] # Close price
        time = new_data[timeframe][:, 6] # Close time
        buy_time, buy_price = pair_trader.times[::2], [trade[2] for trade in pair_trader.trades[::2]]
        sell_time, sell_price = pair_trader.times[1::2], [trade[2] for trade in pair_trader.trades[1::2]]

        fig, axs = plt.subplots(nrows=3, figsize=(19.2, 10.8), dpi=100)
        fig.tight_layout()
        ax1, ax2, ax3 = axs.reshape(-1)
        config_axs(ax1, ax2, ax3)

        ax1.plot(time, close, color='black', linewidth=1)
        ax1.scatter(buy_time, buy_price, color='blue')
        ax1.scatter(sell_time, sell_price, color='red')

        ax2.plot(pair_trader.times, pair_trader._profits, linestyle=':')

        estimations = pair_trader.estimations
        ax3.plot(time[:len(estimations)], estimations)

        plt.show()



if __name__ == '__main__':
    trainer = Trainer()

    trainer.construct_system()

    expert = config.deserialize_expert_from_json()
    expert.show(overview=False)
    expert.set_weights(recursive=True)

    trainer.fit_weights(expert)

    pair_trader = trader.PairTrader('BTC/USDT')
    pair_trader.set_expert(expert)
    trainer.simulate_pair_trader(pair_trader, 360, display=True)