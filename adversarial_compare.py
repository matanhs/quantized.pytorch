import torch as th
import torchvision as tv
from matplotlib import pyplot as plt
import models
from data import get_dataset,limit_ds
from preprocess import get_transform
import os
import tqdm
import logging
from utils.log import setup_logging
from utils.absorb_bn import get_bn_params
from dataclasses import dataclass
import inspect
from utils.misc import Recorder
from utils.meters import MeterDict, OnlineMeter,AverageMeter,accuracy
from functools import partial
from typing import Callable,List,Dict

def _default_matcher_fn(n: str, m: th.nn.Module) -> bool:
    return isinstance(m, th.nn.BatchNorm2d) or isinstance(m, th.nn.Linear)

@dataclass
class Settings:
    def get_args_dict(self):
        ret = {k:getattr(self,k) for k in list(inspect.signature(Settings.__init__).parameters.keys())[1:]}
        ret.update({'spatial_reductions':ret['spatial_reductions'],'include_matcher_fn':str(ret['include_matcher_fn'])})
        return ret

    def __repr__(self):
        return str(self.__class__.__name__) + str(self.get_args_dict())

    def __init__(self, model: str,
                 model_cfg: dict,
                 batch_size: int = 1000,
                 recompute: bool = False,
                 augment_measure: bool = False,
                 augment_test: bool = False,
                 device: str = 'cuda',
                 dataset: str = f'cats_vs_dogs',
                 ckt_path: str = '/home/mharoush/myprojects/convNet.pytorch/results/r18_cats_N_dogs/checkpoint.pth.tar',
                 collector_device: str = 'same',  # use cpu or cuda:<#> if gpu runs OOM
                 limit_test: int = None,
                 limit_measure: int = None,
                 test_split: str = 'val',
                 num_classes: int = 2,
                 alphas : List[float] = [i/500 for i in range(1,100)] + [i/10 for i in range(2,11)],
                 right_sided_fisher_pvalue: bool = True,
                 transform_dataset: str = None,
                 spatial_reductions : Dict[str,Callable[[th.Tensor],th.Tensor]] = None,
                 measure_joint_distribution : bool = False,
                 tag :str = '',
                 ood_datasets : List[str] = None,
                 include_matcher_fn : Callable[[str,th.nn.Module],bool]= _default_matcher_fn):

        self._dict = {}
        arg_names, _, _, local_vars = inspect.getargvalues(inspect.currentframe())
        for name in arg_names[1:]:
            setattr(self, name, local_vars[name])
            self._dict[name] = getattr(self,name)
        if self.ood_datasets is None:
            self.ood_datasets = ['folder-Imagenet_resize', 'folder-LSUN_resize', 'cifar100']
            if self.dataset == 'SVHN':
                self.ood_datasets.insert(0, 'cifar10')
            else:
                self.ood_datasets.insert(0, 'SVHN')

        if self.dataset in self.ood_datasets:
            self.ood_datasets.pop(self.ood_datasets.index(self.dataset))


def Gaussian_KL(mu1, var1, mu2, var2, epsilon=1e-5):
    var1 = var1.clamp(min=epsilon)
    var2 = var2.clamp(min=epsilon)
    return 1 / 2 * (-1 + th.log(var2 / var1) + (var1 + (mu1 - mu2).pow(2)) / var2)


def Gaussian_sym_KL(mu1, sigma1, mu2, sigma2, epsilon=1e-5):
    return 0.5 * (Gaussian_KL(mu1, sigma1, mu2, sigma2, epsilon) + Gaussian_KL(mu2, sigma2, mu1, sigma1, epsilon))


'''
calculate statistics loss
@inputs:
ref_stat_dict - the reference statistics we want to compare against typically we just pass the model state_dict
stat_dict - stats per layer in form {key,(mean,var)}
running_dict - stats per layer in form {key,(running_sum,running_square_sum,num_samples)}
raw_act_dict - calc stats directly from activations {key,act}
'''


def calc_stats_loss(ref_stat_dict=None, stat_dict=None, running_dict=None, raw_act_dict=None, mode='sym', epsilon=1e-8,
                    pre_bn=True, reduce=True):
    # used to aggregate running stats from multiple devices
    def _get_stats_from_running_dict():
        batch_statistics = {}
        for k, v in running_dict.items():
            mean = v[0] / v[2]
            ## var = E(x^2)-(EX)^2: sum_p2/n -sum/n
            var = v[1] / v[2] - mean.pow(2)
            batch_statistics[k] = (mean, var)
        running_dict.clear()
        return batch_statistics

    def _get_stats_from_acts_dict():
        batch_statistics = {}
        for k, act in raw_act_dict.items():
            mean = act.mean((0, 2, 3))
            var = act.var((0, 2, 3))
            batch_statistics[k] = (mean, var)
        return batch_statistics

    ## target statistics provided externally in mu,var form
    if stat_dict:
        batch_statistics = stat_dict
    ## compute mu and var from a running moment accumolation (statistics collected from multi-gpu)
    elif running_dict:
        batch_statistics = _get_stats_from_running_dict()
    # compute mu and var directly from reference activations
    elif raw_act_dict:
        batch_statistics = _get_stats_from_acts_dict()
    else:
        assert 0

    if pre_bn:
        target_mean_key, target_var_key = 'running_mean', 'running_var'
    else:
        target_mean_key, target_var_key = 'bias', 'weight'

    if mode == 'mse':
        calc_stats = lambda m1, m2, v1, v2: m1.sub(m2).pow(2) + v1.sub(v2).pow(2)
    elif mode == 'l1':
        calc_stats = lambda m1, m2, v1, v2: m1.sub(m2).abs() + v1.sub(v2).abs()
    elif mode == 'exp':
        calc_stats = lambda m1, m2, v1, v2: th.exp(m1.sub(m2).abs() + v1.sub(v2).abs())
    elif mode == 'kl':
        calc_stats = lambda m1, m2, v1, v2: Gaussian_KL(m2, v2, m1, v1, epsilon)
    elif mode == 'sym':
        calc_stats = lambda m1, m2, v1, v2: Gaussian_sym_KL(m1, v1, m2, v2, epsilon)
    else:
        assert 0

    # calculate states per layer key in stats dict
    loss_stat = {}
    for i, (k, (m, v)) in enumerate(batch_statistics.items()):
        # collect reference statistics from dictionary
        if ref_stat_dict and k in ref_stat_dict:
                #statistics are in a param dictionary
            ref_dict = ref_stat_dict[k]
            if target_mean_key in ref_dict:
                m_ref, v_ref = ref_dict[target_mean_key], ref_dict[target_var_key]
            elif 'mean:0' in ref_dict:
                m_ref, v_ref = ref_dict[ f'mean:0'], th.diag(ref_dict[ f'cov:0'])
            else:
                assert 0, 'unsuported reference stat dict structure'
        else:
            # assume normal distribution reference
            m_ref, v_ref = th.zeros_like(m), th.ones_like(v)
        m_ref, v_ref = m_ref.to(m.device), v_ref.to(v.device)

        moments_distance_ = calc_stats(m_ref, m, v_ref, v)

        if reduce:
            moments_distance = moments_distance_.mean()
        else:
            moments_distance = moments_distance_

        # if verbose > 0:
        #     with th.no_grad():
        #         zero_sigma = (v < 1e-5).sum()
        #         if zero_sigma > 0 or moments_distance > 5*loss_stat/i:
        #             print(f'high divergence in layer {k}: {moments_distance}'
        #                   f'\nmu:{m.mean():0.4f}<s:r>{m_ref.mean():0.4f}\tsigma:{v.mean():0.4f}<s:r>{v_ref.mean():0.4f}\tsmall sigmas:{zero_sigma}/{len(v)}')
        #
        loss_stat[k] = moments_distance

    if reduce:
        ret_val = 0
        for k, loss in loss_stat:
            ret_val = ret_val + loss
        ret_val = ret_val / len(batch_statistics)
        return ret_val

    return loss_stat

# todo broken for now
def plot(clean_act, fgsm_act, layer_key, reference_stats=None, nbins=256, max_ratio=True, mode='sym',
         rank_by_stats_loss=False):
    if rank_by_stats_loss:
        fgsm_distance = \
        calc_stats_loss(ref_stat_dict=reference_stats, raw_act_dict={layer_key: fgsm_act}, reduce=False, epsilon=1e-8,
                        mode=mode)[layer_key]
        clean_distance = \
        calc_stats_loss(ref_stat_dict=reference_stats, raw_act_dict={layer_key: clean_act}, reduce=False, epsilon=1e-8,
                        mode=mode)[layer_key]
        if max_ratio:
            # normalized ratio to make sure we detect channles that are stable for clean data
            ratio, ids = (fgsm_distance / clean_distance).sort()
        else:
            div, ids = fgsm_distance.sort()

    max_channels = 12
    ncols = 4
    fig, axs = plt.subplots(nrows=max(max_channels // ncols, 1), ncols=ncols)
    for i, channel_id in enumerate(ids[-max_channels:]):
        ax = axs[i // ncols, i % ncols]
        # plt.figure()
        for e, acts in enumerate([clean_act, fgsm_act]):
            ax.hist(acts[:, i].transpose(1, 0).reshape(-1).detach().numpy(), nbins, density=True, alpha=0.3, label=['clean', 'adv'][e])
            ax.tick_params(axis='both', labelsize=5)
            ax.autoscale()

        ax.set_title(f'C-{channel_id}|div-{clean_distance[channel_id]:0.3f}|adv_div-{fgsm_distance[channel_id]:0.3f}',
                     fontdict={'fontsize': 7})
    fig.suptitle(f'{layer_key} sorted by {mode} KL divergence' + (' ratio (adv/clean)' if max_ratio else ''))
    fig.legend()
    fig.set_size_inches(19.2, 10.8)
    fig.show()
    # fig.waitforbuttonpress()
    pass

def gen_inference_fn(ref_stats_dict):
    def _batch_calc(trace_name, m, inputs):
        class_specific_stats = []
        for class_stat_dict in ref_stats_dict:
            reduction_specific_record = {}
            for reduction_name, reduction_stat in class_stat_dict[trace_name[:-8]].items():
                pval_per_input = []
                for e, per_input_stat in enumerate(reduction_stat):
                    ret_channel_strategy={}
                    assert isinstance(per_input_stat,BatchStatsCollectorRet)
                    reduced = per_input_stat.reduction_fn(inputs[e])
                    for channle_reduction_name,rec in per_input_stat.channel_reduction_record.items():
                        ret_channel_strategy[channle_reduction_name] = rec['fn'](reduced)
                    pval_per_input.append(ret_channel_strategy)
                reduction_specific_record[reduction_name] = pval_per_input
            class_specific_stats.append(reduction_specific_record)
        return class_specific_stats
    return _batch_calc


class PvalueMatcher():
    def __init__(self,percentiles,quantiles, two_side=True, right_side=False):
        self.percentiles = percentiles
        self.quantiles = quantiles.t().unsqueeze(0)
        self.num_percentiles = percentiles.shape[0]
        self.right_side = right_side
        self.two_side = (not right_side) and two_side

    ## todo document!
    def __call__(self, x):
        if x.device != self.percentiles.device:
            self.percentiles=self.percentiles.to(x.device)
            self.quantiles = self.quantiles.to(x.device)
        stat_layer = x.unsqueeze(-1).expand(x.shape[0], x.shape[1], self.num_percentiles)
        quant_layer = self.quantiles.expand(stat_layer.shape[0], stat_layer.shape[1], self.num_percentiles)

        ### find p-values based on quantiles
        temp_location = self.num_percentiles - th.sum(stat_layer < quant_layer, -1)
        upper_quant_ind = temp_location > (self.num_percentiles // 2)
        temp_location[upper_quant_ind] += -1
        matching_percentiles = self.percentiles[temp_location]
        if self.two_side:
            matching_percentiles[upper_quant_ind] = 1 - matching_percentiles[upper_quant_ind]
            return matching_percentiles * 2
        if self.right_side:
            return 1-matching_percentiles
        return matching_percentiles

class PvalueMatcherFromSamples(PvalueMatcher):
    def __init__(self, samples, target_percentiles=th.tensor([0.05,
                                                                  0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9,
                                                                  # decision for fisher is right sided
                                                                  0.945, 0.94625, 0.9475, 0.94875,
                                                                  0.95,  # target alpha upper 5%
                                                                  0.95125, 0.9525, 0.95375, 0.955,
                                                                  # add more abnormal percentiles for fusions
                                                                  0.97, 0.98, 0.99, 0.995, 0.999, 0.9995, 0.9999]),
                 two_side=True, right_side=False,):
            num_samples = samples.shape[0]
            adjusted_target_percentiles = (
                    ((target_percentiles + (1 / num_samples / 2)) // (1 / num_samples)) / num_samples
            ).clamp(1 / num_samples, 1 - 1 / num_samples).unique()
            meter = OnlineMeter(batched=True, track_percentiles=True,
                                target_percentiles=adjusted_target_percentiles,
                                per_channel=False, number_edge_samples=10,
                                track_cov=False)
            meter.update(samples)
            logging.debug(
                f'adjusted percentiles {"right tail" if (right_side and not two_side) else "sym"}:\n'
                f'\t{adjusted_target_percentiles.cpu().numpy()}')

            super().__init__(*meter.get_distribution_histogram(),two_side=two_side, right_side=right_side)

def fisher_reduce_all_layers(ref_stats, filter_layer=None, using_ref_record=False, class_id=None):
    # this function summarises all layer pvalues using fisher statistic
    # since we may have multiple channel reduction strategies (e.g. simes, cond-fisher) the strategy dict should have
    # a mapping from reduction output to the actual pvalue (in simes this is just the returned value, for fisher we need
    # to calculate the distribution for each layer statistic)
    sum_pval_per_reduction={}
    for layer_name, layer_stats_dict in ref_stats.items():
        if filter_layer and filter_layer(layer_name):
            continue
        if class_id is not None:
            layer_stats_dict = layer_stats_dict[class_id]
        for spatial_reduction_name, record_per_input in layer_stats_dict.items():
            if spatial_reduction_name not in sum_pval_per_reduction:
                sum_pval_per_reduction[spatial_reduction_name] = {}
                if using_ref_record:
                    channel_reduction_names = record_per_input[0].channel_reduction_record.keys()
                else:
                    channel_reduction_names = record_per_input[0].keys()

                for channel_reduction_name in channel_reduction_names:
                    sum_pval_per_reduction[spatial_reduction_name][channel_reduction_name] = 0.
            # all layer inputs are reduced together for now
            for record in record_per_input:
                if using_ref_record:
                    assert isinstance(record, BatchStatsCollectorRet)
                    for channel_reduction_name ,channel_reduction_record in record.channel_reduction_record.items():
                        pval = channel_reduction_record['record']
                        if 'pval_matcher' in channel_reduction_record:
                            # need to get pvalues first
                            pval = channel_reduction_record['pval_matcher'](pval)
                        # free memory after extracting stats
                        del channel_reduction_record['record']
                        sum_pval_per_reduction[spatial_reduction_name][channel_reduction_name] += -2 * th.log(pval)
                else:
                    for channel_reduction_name, pval in record.items():
                        sum_pval_per_reduction[spatial_reduction_name][channel_reduction_name] += -2 * th.log(pval)

    return sum_pval_per_reduction

def extract_output_distribution_single_class(layer_wise_ref_stats,target_percentiles=th.tensor([0.05,
                                                                      0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9,
                                                                      # decision for fisher is right sided
                                                                      0.945, 0.94625, 0.9475, 0.94875,
                                                                      0.95,# target alpha upper 5%
                                                                      0.95125, 0.9525,0.95375, 0.955,
                                                                      # add more abnormal percentiles for fusions
                                                                      0.97,0.98,0.99, 0.995,0.999,0.9995,0.9999]),
                                right_sided_fisher_pvalue = False,filter_layer=None):
    # reduce all layers (e.g. fisher)
    sum_pval_per_reduction = fisher_reduce_all_layers(layer_wise_ref_stats, filter_layer, using_ref_record=True)
    # update replace fisher output with pvalue per reduction
    fisher_pvals_per_reduction = {}
    for spatial_reduction_name, sum_pval_record in sum_pval_per_reduction.items():
        logging.info(f'\t{spatial_reduction_name}:')
        fisher_pvals_per_reduction[spatial_reduction_name]={}
        # different channle reduction strategies will have different pvalues
        for channel_reduction_name,sum_pval in sum_pval_record.items():
            # use right tail pvalue since we don't care about fisher "normal" looking pvalues that are closer to 0
            kwargs = {'target_percentiles':target_percentiles}
            if right_sided_fisher_pvalue:
                kwargs.update({'two_side':False,'right_side':True})
            fisher_pvals_per_reduction[spatial_reduction_name][channel_reduction_name] = PvalueMatcherFromSamples(samples=sum_pval,**kwargs)
            logging.info(f'\t\t{channel_reduction_name}:\t mean:{sum_pval.mean()}\tstd:{sum_pval.std():0.3f}')
    return fisher_pvals_per_reduction

def extract_output_distribution(all_class_ref_stats,target_percentiles=th.tensor([0.05,
                                                                                  0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9,
                                                                                  # decision for fisher is right sided
                                                                                  0.945, 0.94625, 0.9475, 0.94875,
                                                                                  0.95,# target alpha upper 5%
                                                                                  0.95125, 0.9525,0.95375, 0.955,
                                                                                  # add more abnormal percentiles for fusions
                                                                                  0.97,0.98,0.99, 0.995,0.999,0.9995,0.9999]),
                                right_sided_fisher_pvalue = False,filter_layer=None):
    per_class_record = []
    for e,class_stats_per_layer_dict in enumerate(all_class_ref_stats):
        logging.info(f'Constructing H0 Pvalue matchers for fisher statistic of class {e}/{len(all_class_ref_stats)}')
        fisher_pvals_per_reduction=extract_output_distribution_single_class(class_stats_per_layer_dict,
                                                                            target_percentiles=target_percentiles,
                                                                            right_sided_fisher_pvalue=right_sided_fisher_pvalue,
                                                                            filter_layer=filter_layer)
        per_class_record.append(fisher_pvals_per_reduction)

    return per_class_record

class OODDetector():
    def __init__(self, model, all_class_ref_stats, right_sided_fisher_pvalue=True,
                 include_matcher_fn=lambda n, m: isinstance(m, th.nn.BatchNorm2d) or isinstance(m, th.nn.Linear)):

        self.stats_recorder = Recorder(model, recording_mode=[Recorder._RECORD_INPUT_MODE[1]],
                                       include_matcher_fn=include_matcher_fn,
                                       input_fn=gen_inference_fn(all_class_ref_stats),
                                       recursive=True, device_modifier='same')
        #channle_reduction = ['simes_pval', 'cond_fisher'],
        #self.channel_reduction = channle_reduction
        self.output_pval_matcher = extract_output_distribution(all_class_ref_stats,
                                                               right_sided_fisher_pvalue=right_sided_fisher_pvalue,
                                                               filter_layer=lambda n: n not in self.stats_recorder.tracked_modules.keys())
        self.num_classes = len(all_class_ref_stats)


    # helper function to convert per class per reduction to per reduction per class dictionary
    def _gen_output_dict(self,per_class_per_reduction_record):
        # prepare a dict with pvalues per reduction per sample per class i.e. {reduction_name : (BxC)}
        reduction_stats_collection = {}
        for class_stats in per_class_per_reduction_record:
            for reduction_name, pval in class_stats.items():
                if reduction_name in reduction_stats_collection:
                    reduction_stats_collection[reduction_name] = th.cat([reduction_stats_collection[reduction_name],
                                                                         pval.unsqueeze(1)],1)
                else:
                    reduction_stats_collection[reduction_name] = pval.unsqueeze(1)

        return reduction_stats_collection

    # this function should return pvalues in the format of (Batch x num_classes)
    # todo merge this with extract_output_distribution fisher compute (iterate over tracked modules
    #  instead of record entries)
    def get_fisher(self):
        per_class_record = []
        # reduce all layers (e.g. fisher)
        for class_id in range(self.num_classes):
            sum_pval_per_reduction = fisher_reduce_all_layers(self.stats_recorder.record,class_id=class_id,using_ref_record=False)
            # update fisher pvalue per reduction
            fisher_pvals_per_reduction = {}
            for reduction_name, sum_pval_record in sum_pval_per_reduction.items():
                for s, sum_pval in sum_pval_record.items():
                    fisher_pvals_per_reduction[f'{reduction_name}_{s}'] = self.output_pval_matcher[class_id][reduction_name][s](sum_pval)

            per_class_record.append(fisher_pvals_per_reduction)

        self.stats_recorder.record.clear()
        return self._gen_output_dict(per_class_record)

# clac Simes per batch element (samples x variables)
def calc_simes(pval):
    pval, _ = th.sort(pval, 1)
    rank = th.arange(1, pval.shape[1] + 1,device=pval.device).repeat(pval.shape[0], 1)
    simes_pval, _ = th.min(pval.shape[1] * pval / rank, 1)
    return simes_pval.unsqueeze(1)

def calc_cond_fisher(pval, thresh=0.5):
    pval[pval>thresh]=1
    return -2*pval.log().sum(1).unsqueeze(1)

# rescaled fisher test
def calc_mean_fisher(pval):
    return -2*pval.log().mean(1).unsqueeze(1)

def spatial_mean(x):
    return x.mean(tuple(range(2, x.dim()))) if x.dim() > 2 else x

def dim_reducer(x):
    if x.dim() <= 3:
        return x

    return x.view(x.shape[0], x.shape[1], -1)

# k will ignore k-1 most extreme values, todo choose value to match percentile ()
def spatial_edges(x,k=1,is_max=True,):
    if x.dim() < 3:
        return x
    x_ = dim_reducer(x)

    ret = x_.topk(k, -1, is_max)[0]
    if is_max:
        return ret[:,:,0]

    return ret[:,:,k-1]

def spatial_min(x,k=1):
    return spatial_edges(x,k,False)

def spatial_max(x,k=1):
    return spatial_edges(x,k,True)

def spatial_margin(x,k=1):
    return spatial_max(x,k)-spatial_min(x,k)

def spatial_fuse(x,k=1):
    return 0.5*(spatial_mean(x)+spatial_margin(x,k))

def spatial_l2(x):
    if x.dim() < 3:
        return x
    return th.norm(dim_reducer(x), dim=-1)


class PickleableFunctionComposition():
    def __init__(self,f1, f2):
        self.f1 = f1
        self.f2 = f2

    def __call__(self,x):
        return self.f2(self.f1(x))


class MahalanobisDistance():
    def __init__(self,mean,inv_cov):
        self.mean = mean
        self.inv_cov = inv_cov

    def __call__(self,x):
        x_c = x-self.mean
        return (x_c.matmul(self.inv_cov).matmul(x_c.t())).diag().unsqueeze(1)


## auxilary data containers
@dataclass()
class BatchStatsCollectorRet():
    def __init__(self, reduction_name: str,
                 reduction_fn = lambda x: x,
                 cov: th.Tensor = None,
                 num_observations: int = 0,
                 meter :AverageMeter = None):
        self.reduction_name = reduction_name
        self.reduction_fn = reduction_fn
        ## collected stats
        self.cov = cov
        self.num_observations = num_observations
        # spatial reduction meter
        self.meter = meter
        # record to hold all information on channel reduction methods
        self.channel_reduction_record = {}


@dataclass()
class BatchStatsCollectorCfg():
    cov_off : bool = False # True
    _track_cov: bool = False
    # using partial stats for mahalanobis covariance estimate
    partial_stats: bool = True # False
    update_tracker: bool = True
    find_simes: bool = False
    find_cond_fisher: bool = False
    mahalanobis : bool = False
    target_percentiles = th.tensor([0.001, 0.002, 0.005,0.01,
                          # estimate more percentiles next to the target alpha
                          0.02, 0.023, 0.024,0.025,0.026, 0.027 ,0.03,
                          # collect intervals for better layer reduction statistic approximation
                          0.045,0.047,0.049,0.05,0.051,0.053,0.055, 0.07, 0.1, 0.2, 0.3, 0.4, 0.5]) # percentiles will be mirrored
    num_edge_samples: int = 100

    def __init__(self,batch_size,reduction_dictionary = None,include_matcher_fn = None):
        # which reductions to use ?
        self.reduction_dictionary = reduction_dictionary or {
            'spatial-mean': spatial_mean,
            'spatial-max': spatial_max,
            'spatial-min': spatial_min,
            'spatial-margin': partial(spatial_margin, k=1),
            # 'spatial-l2':spatial_l2
        }
        # which layers to collect?
        self.include_matcher_fn = include_matcher_fn or (lambda n, m: isinstance(m, th.nn.BatchNorm2d) or isinstance(m, th.nn.Linear))
        assert 0.5 == self.target_percentiles[-1], 'tensor must include median'
        self.target_percentiles = th.cat([self.target_percentiles, (1 - self.target_percentiles).sort()[0]])
        # adjust percentiles to the specified batch size
        self.target_percentiles = (((self.target_percentiles+(1/batch_size/2)) // (1/batch_size))/batch_size ).unique()
        logging.info(f'measure target percentiles {self.target_percentiles.numpy()}')


def measure_data_statistics(loader, model, epochs=5, model_device='cuda', collector_device='same', batch_size=1000,
                            measure_settings : BatchStatsCollectorCfg = None):

    measure_settings = measure_settings or BatchStatsCollectorCfg(batch_size)
    compute_cov_on_partial_stats = measure_settings.partial_stats and not measure_settings.cov_off
    ## bypass the simple recorder dictionary with a meter dictionary to track per layer statistics
    tracker = MeterDict(meter_factory=lambda k, v: OnlineMeter(batched=True, track_percentiles=True,
                                                               target_percentiles=measure_settings.target_percentiles,
                                                               per_channel=True,number_edge_samples=measure_settings.num_edge_samples,
                                                               track_cov=compute_cov_on_partial_stats))

    # function collects statistics of a batched tensors, return the collected statistics per input tensor
    def _batch_stats_collector(trace_name, m, inputs):
        stats_per_input = []
        for e, i in enumerate(inputs):
            reduction_specific_record = []
            for reduction_name, reduction_fn in measure_settings.reduction_dictionary.items():
                tracker_name = f'{trace_name}_{reduction_name}:{e}'
                ## make sure input is a 2d tensor [batch, nchannels]
                i_ = reduction_fn(i)
                if collector_device != 'same' and collector_device != model_device:
                    i_ = i_.to(collector_device)

                num_observations, channels = i_.shape
                reduction_ret_obj = BatchStatsCollectorRet(reduction_name,reduction_fn,num_observations=num_observations)

                # we dont always want to update the tracker, particularly when we call the method again to collect
                # statistics that depends on previous values that are computed on the entire measure dataset
                if measure_settings.update_tracker:
                    # tracker keeps track statistics such as number of samples seen, mean, var, percentiles and potentially
                    # running mean covariance
                    tracker.update({tracker_name: i_})
                # save a reference to the meter for convenience
                reduction_ret_obj.meter = tracker[tracker_name]
                ## typically second phase measurements
                # this requires first collecting reduction statistics (covariance), then in a second pass we can collect
                if measure_settings.mahalanobis:
                    mahalanobis_fn = MahalanobisDistance(tracker[tracker_name].mean,tracker[tracker_name].inv_cov)
                    # reduce all per channels stats to a single score
                    i_m = mahalanobis_fn(i_)
                    # measure the distribution per layer
                    tracker.update({f'{tracker_name}-@mahalabobis': i_m})
                    reduction_ret_obj.channel_reduction_record.update( {'mahalanobis':
                                                                           # used for layer fusion (concatinate over all batches)
                                                                          {'record' : i_m,
                                                                           ## used to extract the pval from the output of the spatial reduction output
                                                                           # channel reduction transformation
                                                                           'right_side_pval': True,
                                                                           'fn' : mahalanobis_fn,
                                                                           # meter for the channel reduction (used to create pval matcher)
                                                                           'meter': tracker[f'{tracker_name}-@mahalabobis'],
                                                                           }
                                                          } )

                if measure_settings.find_simes or measure_settings.find_cond_fisher:
                    if not hasattr(reduction_ret_obj.meter,'pval_matcher'):
                        p,q=reduction_ret_obj.meter.get_distribution_histogram()
                        reduction_ret_obj.meter.pval_matcher = PvalueMatcher(percentiles=p,quantiles=q)
                    # here we first seek the pvalue for the observated reduction value
                    pval = reduction_ret_obj.meter.pval_matcher(i_)

                    if measure_settings.find_simes :
                        reduction_ret_obj.channel_reduction_record.update({'simes_c':
                                                                   {
                                                                   'right_side_pval': False,
                                                                   'record':calc_simes(pval),
                                                                   'fn': PickleableFunctionComposition(f1=reduction_ret_obj.meter.pval_matcher, f2=calc_simes)
                                                                   }
                                                               })

                    if measure_settings.find_cond_fisher:
                        fisher_out = calc_cond_fisher(pval)
                        # result is not normalized as pvalues, we need to measure the distribution
                        # of this value to return to pval terms
                        tracker.update({f'{tracker_name}-@fisher_c': fisher_out})
                        reduction_ret_obj.channel_reduction_record.update({'fisher_c':
                                                                   {'record': fisher_out,
                                                                    'meter':tracker[f'{tracker_name}-@fisher_c'],
                                                                    'right_side_pval':True,
                                                                    'fn':PickleableFunctionComposition(f1=reduction_ret_obj.meter.pval_matcher, f2=calc_cond_fisher)
                                                                    }
                                                              })

                # calculate covariance, can be used to compute covariance with global mean instead of running mean
                if measure_settings._track_cov:
                    _i_mean = reduction_ret_obj.meter.mean
                    _i_centered = i_ - _i_mean
                    reduction_ret_obj.cov = _i_centered.transpose(1, 0).matmul(_i_centered) / (num_observations)

                reduction_specific_record.append(reduction_ret_obj)

            stats_per_input.append(reduction_specific_record)

        return stats_per_input

    # this functionality is used to calculate a more accurate covariance estimate
    def _batch_stats_reducer(old_record, new_entry):
        stats_per_input = []
        for input_id, reduction_stats_record_n in enumerate(new_entry):
            reductions_per_input = []
            for reduction_id, new_reduction_ret_obj in enumerate(reduction_stats_record_n):
                reduction_ret_obj = old_record[input_id][reduction_id]
                assert reduction_ret_obj.reduction_name == new_reduction_ret_obj.reduction_name
                # compute global mean covariance update
                if new_reduction_ret_obj.cov is not None:
                    reduction_ret_obj.num_observations += new_reduction_ret_obj.num_observations
                    scale = new_reduction_ret_obj.num_observations / reduction_ret_obj.num_observations
                    # delta
                    delta = new_reduction_ret_obj.cov.sub(reduction_ret_obj.cov)
                    # update mean covariance
                    reduction_ret_obj.cov.add_(delta.mul_(scale))
                    reduction_ret_obj.meter.cov = reduction_ret_obj.cov

                # aggregate all observed channel reduction values per method
                for channel_reduction_name in new_reduction_ret_obj.channel_reduction_record.keys():
                    reduction_ret_obj.channel_reduction_record[channel_reduction_name]['record'] = \
                        th.cat([reduction_ret_obj.channel_reduction_record[channel_reduction_name]['record'],
                                new_reduction_ret_obj.channel_reduction_record[channel_reduction_name]['record']])

                reductions_per_input.append(reduction_ret_obj)
            stats_per_input.append(reductions_per_input)
        return stats_per_input

    #simple loop over measure data to collect statistics
    def _loop_over_data():
        model.eval()
        with th.no_grad():
            for _ in tqdm.trange(epochs):
                for d, l in loader:
                    _ = model(d.to(model_device))

    model.to(model_device)
    r = Recorder(model, recording_mode=[Recorder._RECORD_INPUT_MODE[1]],
                 include_matcher_fn=measure_settings.include_matcher_fn,
                 input_fn=_batch_stats_collector,
                 activation_reducer_fn=_batch_stats_reducer, recursive=True, device_modifier='same')

    # if compute_cov_on_partial_stats:
    #     # todo compare aginst meter cov
    #     measure_settings._track_cov = True

    logging.info(f'\t\tmeasuring {"covariance " if compute_cov_on_partial_stats else ""} mean and percentiles')
    _loop_over_data()
    logging.info(f'\t\tcalculating {"covariance and " if measure_settings._track_cov and not compute_cov_on_partial_stats else ""}Simes pvalues using measured mean and quantiles')
    measure_settings.update_tracker = False
    measure_settings.mahalanobis = True
    measure_settings.update_channel_trackers = False
    measure_settings.find_simes = True
    measure_settings.find_cond_fisher = True
    measure_settings._track_cov = not (measure_settings._track_cov or measure_settings.cov_off)
    r.record.clear()
    _loop_over_data()

    ## build reference dictionary with per layer information per reduction (reversing collection order)
    ret_stat_dict = {}
    for k in r.tracked_modules.keys():
        ret_stat_dict[k] = {}
        for kk, stats_per_input in r.record.items():
            if kk.startswith(k):
                for inp_id, reduction_records in enumerate(stats_per_input):
                    for reduction_record in reduction_records:
                        assert isinstance(reduction_record,BatchStatsCollectorRet)
                        # #todo create a channel reduction pval matcher right here
                        for channel_reduction_entry in reduction_record.channel_reduction_record.values():
                            channel_reduction_entry['record'] = channel_reduction_entry['record'].cpu()
                            if 'meter' in channel_reduction_entry:
                                p,q = channel_reduction_entry['meter'].get_distribution_histogram()
                                pval_matcher = PvalueMatcher(quantiles=q,percentiles=p,right_side=channel_reduction_entry['right_side_pval'])
                                channel_reduction_entry['pval_matcher'] = pval_matcher
                                # create the final function to retrive the layer pvalue from a given spatial reduction
                                channel_reduction_entry['fn'] = PickleableFunctionComposition(channel_reduction_entry['fn'], pval_matcher)

                        if reduction_record.reduction_name in ret_stat_dict[k]:
                            ret_stat_dict[k][reduction_record.reduction_name] += [reduction_record]
                        else:
                            ret_stat_dict[k][reduction_record.reduction_name] = [reduction_record]
    r.record.clear()
    r.remove_model_hooks()
    return ret_stat_dict

def batched_meter_factory(k, v):
    return OnlineMeter(batched=True)

def evaluate_data(loader,model, detector,model_device,alpha_list = None,in_dist=False):
    alpha_list = alpha_list or [0.05]
    def _gen_curve(pvalues_for_val):
        rejected_ = []
        for alpha_ in alpha_list:
            rejected_.append((pvalues_for_val < alpha_).float().unsqueeze(1))
        return th.cat(rejected_, 1)
    model.eval()
    model.to(model_device)
    top1 = AverageMeter()
    top5 = AverageMeter()

    rejected = {}
    with th.no_grad():
        for d, l in tqdm.tqdm(loader, total=len(loader)):
            out = model(d.to(model_device)).cpu()
            predicted = out.argmax(1)
            pvalues_dict = detector.get_fisher()
            last_reduction_pvalues = {}
            if in_dist:
                correct_predictions = l == predicted
                t1, t5 = accuracy(out, l, (1, 5))
                top1.update(t1, out.shape[0])
                top5.update(t5, out.shape[0])
                logging.info(f'\nModel Prec@1 {top1.avg:.3f} ({top1.std:.3f}) \t'
                             f'Prec@5 {top5.avg:.3f} ({top5.std:.3f})')
            for reduction_name,pvalues in pvalues_dict.items():
                pvalues=pvalues.squeeze().cpu()
                # aggragate pvalues or return per reduction score
                best_class_pval, best_class_pval_id = pvalues.max(1)
                class_conditional_pval = pvalues[th.arange(l.shape[0]), predicted]
                # measure rejection rates for a range of pvalues under each measure and each reduction
                if reduction_name not in rejected:
                    rejected[reduction_name] = MeterDict(meter_factory=batched_meter_factory)
                # keep track of pvalue predictions
                last_reduction_pvalues[reduction_name]={
                    'class_conditional_pval': class_conditional_pval,
                    'max_pval': best_class_pval,
                    'max_pval_id': best_class_pval_id
                }

                rejected[reduction_name].update({
                    'class_conditional_pval': _gen_curve(class_conditional_pval),
                    'max_pval': _gen_curve(best_class_pval),
                })
                if in_dist:
                    correct_pred_conditional_pval = class_conditional_pval[correct_predictions]
                    true_class_pval = pvalues[th.arange(l.shape[0]), l]
                    last_reduction_pvalues[reduction_name].update({
                        'true_class_pval' : true_class_pval,
                        'class_conditional_correct_only_pval' : correct_pred_conditional_pval
                    })

                    rejected[reduction_name].update({
                        'true_class_pval' : _gen_curve(true_class_pval),
                        'class_conditional_correct_only_pval' : _gen_curve(correct_pred_conditional_pval)
                    })

            ## in this section we can fuse different reductions to produce a potentially stronger rejection method
            # if 'fused_pval_min_max' not in rejected:
            #     # prime fusion meters
            #     for fusion in ['fused_pval_mean_margin','fused_pval_min_max','fused_pval_min_max_mean', 'fused_pval_all']:
            #         rejected[fusion]=MeterDict(meter_factory=batched_meter_factory)
            #
            # # for now we only do this for max-pval rejection method
            # fused_pval = calc_simes(th.stack([last_reduction_pvalues['spatial-mean']['max_pval'],
            #                      last_reduction_pvalues['spatial-margin']['max_pval']], 1)).squeeze()
            # rejected['fused_pval_mean_margin'].update({'max_pval': _gen_curve(fused_pval)})
            #
            # fused_pval = calc_simes(th.stack([last_reduction_pvalues['spatial-min']['max_pval'],
            #                                   last_reduction_pvalues['spatial-max']['max_pval']], 1)).squeeze()
            # rejected['fused_pval_min_max'].update({'max_pval': _gen_curve(fused_pval)})
            #
            # fused_pval = calc_simes(th.stack([last_reduction_pvalues['spatial-mean']['max_pval'],
            #                                   last_reduction_pvalues['spatial-min']['max_pval'],
            #                                   last_reduction_pvalues['spatial-max']['max_pval']], 1)).squeeze()
            # rejected['fused_pval_min_max_mean'].update({'max_pval': _gen_curve(fused_pval)})
            #
            # fused_pval = calc_simes(th.stack([last_reduction_pvalues['spatial-margin']['max_pval'],
            #                                   last_reduction_pvalues['spatial-mean']['max_pval'],
            #                                   last_reduction_pvalues['spatial-min']['max_pval'],
            #                                   last_reduction_pvalues['spatial-max']['max_pval']], 1)).squeeze()
            # rejected['fused_pval_all'].update({'max_pval': _gen_curve(fused_pval)})

            ## end batch report
            # report accuracy and pvalue stats for in-dist dataset
            if in_dist:
                t1_likely, t5_likely = accuracy(pvalues.squeeze(), l, (1, 5))
                false_pred_pvalus = pvalues[th.logical_not(correct_predictions)]
                logging.info(f'Prec@1 {t1_likely:0.3f}, Prec@5 {t5_likely:.3f}'
                             f'\tFalse pred avg pvalue {false_pred_pvalus.float().mean():.3f}')
            # per reduction summary
            for reduction_name in rejected.keys():
                logging.info(f'{reduction_name}:')
                if in_dist and reduction_name in last_reduction_pvalues:
                    # additional in-dist results per reduction
                    best_class_pval_id = last_reduction_pvalues[reduction_name]['max_pval_id']
                    # model prediction agrees with selected pvalue
                    agreement = (best_class_pval_id == predicted)
                    # agreement only on predictions that are correct
                    agreement_true = (agreement == correct_predictions)
                    logging.info(f'\tagreement: {agreement.float().mean():.3f},'
                                 f'\tagreement on true: {agreement_true.float().mean():.3f}')
                # reduction rejected report
                logging.info(f'\tRejected: {rejected[reduction_name]["max_pval"].mean.numpy()[20:30]}')

        ## end of eval report
        if in_dist:
            logging.info(f'\nModel prediction :Prec@1 {top1.avg:.3f} ({top1.std:.3f}) \t'
                         f'Prec@5 {top5.avg:.3f} ({top5.std:.3f})')
        ## pack results
        ret_dict = {}
        for reduction_name, rejected_p in rejected.items():
            logging.info(f'{reduction_name} rejected: {rejected[reduction_name]["max_pval"].mean.numpy()[:10]}')
            ## strip meter dict functionality for simpler post-processing
            reduction_dict = {}
            for k,v in rejected_p.items():
                # keeping meter object - potentially remove it here
                reduction_dict[k] = v
            ret_dict[reduction_name] = reduction_dict
    return ret_dict

    # Important! recorder hooks should be removed when done

def result_summary(res_dict,args_dict):
    ## if not configured setup logging for external caller
    if not logging.getLogger('').handlers:
        setup_logging()
    in_dist=args_dict['dataset']
    alphas = args_dict['alphas']
    logging.info(f'Report for {args_dict["model"]} - {in_dist}')
    for red, vd in res_dict[in_dist].items():
        logging.info(red)
        for n, p in vd.items():
            rejected = {}
            pvalues_under_alpha = [i for i in p.mean.tolist() if i < 0.051]
            pvalues_id = len(pvalues_under_alpha)
            for k, ood_v in res_dict.items():
                if k != in_dist and n in ood_v[red]:
                    rejected[k] = (ood_v[red][n].mean[max(pvalues_id- 1,0)].numpy())
            res= p.mean[0] if pvalues_id==0 else pvalues_under_alpha[pvalues_id- 1]
            logging.info(f'\t {n}: {res:.3f} ({alphas[pvalues_id]})')
            for kk, vv in rejected.items():
                logging.info(f'\t\t{kk}: {vv:0.3f}')

def report_from_file(path):
    res = th.load(path,map_location='cpu')
    result_summary(res['results'], res['settings'])

def measure_and_eval(args : Settings):
    rejection_results = {} # dataset , out
    model = getattr(models,args.model)(**(args.model_cfg))
    checkpoint = th.load(args.ckt_path, map_location='cpu')
    if 'state_dict' in checkpoint:
        checkpoint=checkpoint['state_dict']

    model.load_state_dict(checkpoint)


    expected_transform_measure = get_transform(args.transform_dataset or args.dataset, augment=args.augment_measure)
    expected_transform_test = get_transform(args.transform_dataset or args.dataset, augment=args.augment_test)
    ## this part is meant to improve percentile collection for cifar100 where the number of samples per class is small
    # if args.dataset == 'cifar100':
    #     import torchvision.transforms.transforms as ttf
    #     expected_transform_measure = get_transform(args.transform_dataset or args.dataset, augment=True)
    #     expected_transform_measure.transforms[0] = ttf.RandomResizedCrop((32, 32), scale=(0.8, 1))

    if args.augment_measure:
        epochs = 5
    else:
        epochs = 1

    calibrated_path = f'measured_stats_per_class-{args.model}-{args.dataset}-{"augment" if args.augment_measure else "no_augment"}{"_joint" if args.measure_joint_distribution else ""}{args.tag}.pth'
    if not args.recompute and os.path.exists(calibrated_path):
        all_class_ref_stats = th.load(calibrated_path,map_location=args.device)
    else:
        ds = get_dataset(args.dataset, 'train', expected_transform_measure)
        if args.measure_joint_distribution:
            classes = ['all']
        else:
            classes = ds.classes if hasattr(ds,'classes') else range(args.num_classes)
        if args.limit_measure:
            ds=limit_ds(ds,args.limit_measure,per_class=True)
        all_class_ref_stats=[]
        targets = th.tensor(ds.targets) if hasattr(ds,'targets') else  th.tensor(ds.labels)
        for class_id,class_name in enumerate(classes):
            logging.info(f'\t{class_id}/{len(classes)}\tcollecting stats for class {class_name}')
            if not args.measure_joint_distribution:
                ds_ = th.utils.data.Subset(ds,th.where(targets==class_id)[0])
            else:
                ds_ = ds
            sampler = None #th.utils.data.RandomSampler(ds_,replacement=True,num_samples=epochs*args.batch_size)
            train_loader = th.utils.data.DataLoader(
                    ds_, sampler=sampler,
                    batch_size=args.batch_size, shuffle=False if sampler else True,
                    num_workers=8, pin_memory=True, drop_last=False)
            measure_settings = BatchStatsCollectorCfg(args.batch_size,reduction_dictionary=args.spatial_reductions,
                                                      # todo: maybe split measure and test include fn in Settings
                                                      include_matcher_fn=args.include_matcher_fn)
            class_stats=measure_data_statistics(train_loader, model, epochs=epochs,
                                                               model_device=args.device,
                                                               collector_device=args.collector_device,
                                                               batch_size=args.batch_size,
                                                               measure_settings=measure_settings)
            all_class_ref_stats.append(class_stats)
        logging.info('saving reference stats dict')
        th.save(all_class_ref_stats, calibrated_path)

    val_ds = get_dataset(args.dataset, args.test_split ,expected_transform_test)
    if args.limit_test:
        val_ds = limit_ds(val_ds,args.limit_test,per_class=False)
    # get the pvalue estimators per class per reduction for the channel and final output functions
    # here we can implement subsample channles and other layer reductions exept Fisher
    # todo add Specs to fuse the pvalue matcher and OODDetector as they are tightly coupled.
    detector = OODDetector(model,all_class_ref_stats,right_sided_fisher_pvalue=args.right_sided_fisher_pvalue,
                           include_matcher_fn=args.include_matcher_fn)

    # todo replace all targets to in or out of distribution?
    # todo add adversarial samples test
    # run in-dist data evaluate per class to simplify analysis
    logging.info(f'evaluating inliers')
    #for class_id,class_name in enumerate(val_ds.classes):
    #    sampler = th.utils.data.SubsetRandomSampler(th.where(targets==class_id)[0]) #th.utils.data.RandomSampler(ds, replacement=True,num_samples=5000)
    sampler = None
    val_loader = th.utils.data.DataLoader(
            val_ds, sampler=sampler,
            batch_size=args.batch_size, shuffle=False,
            num_workers=8, pin_memory=True, drop_last=True)
    rejection_results[args.dataset]=evaluate_data(val_loader, model, detector,args.device,alpha_list=args.alphas,in_dist=True)
    logging.info(f'evaluating outliers')

    for ood_dataset in args.ood_datasets:
        ood_ds = get_dataset(ood_dataset, 'val',expected_transform_test)
        if args.limit_test:
            ood_ds = limit_ds(ood_ds,args.limit_test,per_class=False)
        ood_loader = th.utils.data.DataLoader(
            ood_ds, sampler=None,
            batch_size=args.batch_size, shuffle=False,
            num_workers=8, pin_memory=False, drop_last=True)
        logging.info(f'evaluating {ood_dataset}')
        rejection_results[ood_dataset] = evaluate_data(ood_loader, model, detector, args.device,alpha_list=args.alphas)

    th.save({'results':rejection_results,'settings':args.get_args_dict()},f'experiment_results-{args.model}-{args.dataset}{args.tag}.pth')
    result_summary(rejection_results,args.get_args_dict())

## limit layers used to a subset (variace reduction), replace this function with your own (this filter is
# specifically for denesent)
def include_densenet_even_layers_fn(n, m):
    #n is the trace name, m is the actual module which can be used to target modules with specific attributes
    return isinstance(m, th.nn.BatchNorm2d) and n.split('.')[-1].endswith('2') or isinstance(m, th.nn.Linear)
# helper functions for more expressive layer filtering
class WhiteListInclude():
    def __init__(self,layer_white_list):
        self.layer_white_list = layer_white_list
    def __call__(self,n, m):
        return n in self.layer_white_list

import re
class RegxInclude():
    def __init__(self,pattern):
        self.pattern = pattern

    def __call__(self,n, m):
        # n is the trace name, m is the actual module which can be used to target modules with specific attributes
        return bool(re.fullmatch(self.pattern,n))

# reminder we look at the input of layers, following layers used by mhalanobis paper
densenet_mahalanobis_matcher_fn = WhiteListInclude(['block1', 'block2','block3','avg_pool'])
resnet_mahalanobis_matcher_fn = WhiteListInclude(['layer1', 'layer2','layer3','layer4','avg_pool'])
if __name__ == '__main__':
    exp_id = 0
    device_id = exp_id % th.cuda.device_count()
    recompute = True
    tag=''
    # resnet18_cats_dogs = Settings(
    #     model='resnet',
    #     dataset='cats_vs_dogs',
    #     model_cfg={'dataset': 'imagenet', 'depth': 18, 'num_classes': 2},
    #     ckt_path='/home/mharoush/myprojects/convNet.pytorch/results/r18_cats_N_dogs/checkpoint.pth.tar',
    #     device=f'cuda:{device_id}'
    # )
    class R34ExpGroupSettings(Settings):
        def __init__(self,**kwargs):
            super().__init__(model='ResNet34', #include_matcher_fn=resnet_mahalanobis_matcher_fn,, tag='-@mahalanobis',
                             device=f'cuda:{device_id}',
                             augment_measure=False,recompute=recompute, **kwargs)
    resnet34_cifar10 = R34ExpGroupSettings(
        dataset='cifar10',
        num_classes=10,
        model_cfg={'num_c': 10},
        ckt_path='/home/mharoush/myprojects/Residual-Flow/pre_trained/resnet_cifar10.pth',
    )

    resnet34_cifar100 = R34ExpGroupSettings(
        dataset='cifar100',
        num_classes=100,
        model_cfg={'num_c': 100},
        batch_size=500,
        ckt_path='/home/mharoush/myprojects/Residual-Flow/pre_trained/resnet_cifar100.pth',
    )

    resnet34_svhn = R34ExpGroupSettings(
        dataset='SVHN',
        num_classes=10,
        model_cfg={'num_c': 10},
        batch_size=1000,
        ckt_path='/home/mharoush/myprojects/Residual-Flow/pre_trained/resnet_svhn.pth',
    )
    class DN3ExpGroupSettings(Settings):
        def __init__(self,**kwargs):
            super().__init__(model='DenseNet3', #include_matcher_fn=include_densenet_even_layers_fn,
                             device=f'cuda:{exp_id % th.cuda.device_count()}', tag=tag,recompute=recompute, **kwargs)
    densenet_cifar10 = DN3ExpGroupSettings(
        dataset='cifar10',
        num_classes=10,
        model_cfg={'num_classes': 10,'depth':100},
        ckt_path='densenet_cifar10_ported.pth',
    )

    densenet_cifar100 = DN3ExpGroupSettings(
        dataset='cifar100',
        num_classes=100,
        model_cfg={'num_classes': 100,'depth':100},
        ckt_path='densenet_cifar100_ported.pth',
        batch_size=500,
        )

    densenet_svhn = DN3ExpGroupSettings(
        dataset='SVHN',
        num_classes = 10,
        model_cfg={'num_classes': 10,'depth':100},
        ckt_path='densenet_svhn_ported.pth',
    )

    # # this can be used used to produced measure stats dict on ood data for a given model
    # densenet_svhn_cross_cifar10 = ExpGroupSettings(
    #     model='DenseNet3',
    #     dataset='cifar10',
    #     transform_dataset='SVHN',
    #     num_classes = 10,
    #     model_cfg={'num_classes': 10,'depth':100},
    #     ckt_path='densenet_svhn_ported.pth',
    #     include_matcher_fn=densenet_mahalanobis_matcher_fn
    # )

    # if device_id%2==0:
    #     experiments = [resnet34_cifar10, resnet34_cifar100, resnet34_svhn]
    # else:
    #     experiments = [densenet_cifar10, densenet_cifar100, densenet_svhn]
    #experiments = [densenet_svhn_cross_cifar10]
    experiments = [resnet34_cifar10, resnet34_cifar100, resnet34_svhn, densenet_cifar10, densenet_cifar100, densenet_svhn]
    experiments = [experiments[exp_id]]
    setup_logging()
    for args in experiments:
        logging.info(args)
        measure_and_eval(args)
pass

# record = th.load(f'record-{args.dataset}.pth')
# adv_tag = 'FGSM_0.1'
# layer_inputs = recorded[layer_name]
# layer_inputs_fgsm = recorded[f'{layer_name}-@{adv_tag}']
# def _maybe_slice(tensor, nsamples=-1):
#     if nsamples > 0:
#         return tensor[0:nsamples]
#     return tensor
#
# for layer_name in args.plot_layer_names:
#     clean_act = _maybe_slice(record[layer_name + '_forward_input:0'])
#     fgsm_act = _maybe_slice(record[layer_name + f'_forward_input:0-@{adv_tag}'])
#
#     plot(clean_act, fgsm_act, layer_name, reference_stats=ref_stats, rank_by_stats_loss=True, max_ratio=False)
# plt.waitforbuttonpress()
