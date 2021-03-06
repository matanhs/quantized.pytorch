import argparse
import os
#import subprocess
import time
import logging
import torch
import shutil
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from utils.absorb_bn import search_absorbe_bn
from utils.mixup import MixUp
import models
from data import get_dataset
from torchvision.transforms import Compose
from torchvision import models as tvmodels
from preprocess import get_transform,RandomNoise,Cutout,ImgGhosting
from utils.log import setup_logging, ResultsLog, save_checkpoint
from utils.meters import AverageMeter, accuracy
from utils.optim import OptimRegime
from utils.misc import torch_dtypes,CosineSimilarityChannelWiseLoss
from datetime import datetime
from ast import literal_eval
from models.modules.quantize import set_measure_mode,set_bn_is_train,freeze_quant_params,\
    set_global_quantization_method,QuantMeasure,is_bn,overwrite_params,set_quant_mode, QReWriter
_DEFUALT_W_NBITS = 4
_DEFUALT_A_NBITS = 8

model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models.__dict__[name]))
model_names = sorted(name for name in tvmodels.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(tvmodels.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ConvNet Training')
###GENERAL
parser.add_argument('--args-from-file', default=None,
                    help='load run arguments from file')
parser.add_argument('--results_dir', metavar='RESULTS_DIR', default='./results',
                    help='results dir')
parser.add_argument('--save', metavar='SAVE', default='',
                    help='saved folder')
parser.add_argument('--exp-group', default=None,
                    help='use a shared file to collect a group of experiment results')
parser.add_argument('--print-freq', '-pf', default=100, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--ckpt-freq', '-cf', default=10, type=int,
                    metavar='N', help='save checkpoint frequency (default: 10)')
parser.add_argument('--seed', default=123, type=int,
                    help='random seed (default: 123)')
####OP MOD
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', type=str, metavar='FILE',
                    help='evaluate model FILE on validation set')
parser.add_argument('--device', default='cuda',
                    help='device assignment ("cpu" or "cuda")')
parser.add_argument('--device_ids', default=[0], type=int, nargs='+',
                    help='device ids assignment (e.g 0 1 2 3')
parser.add_argument('--dtype', default='float',
                    help='type of tensor: ' +
                         ' | '.join(torch_dtypes.keys()) +
                         ' (default: float)')
###MODEL
parser.add_argument('--model', '-a', metavar='MODEL', default='alexnet',
                    choices=model_names,
                    help='model architecture: ' +
                    ' | '.join(model_names) +
                    ' (default: alexnet)')
parser.add_argument('--model_config', default='',
                    help='additional architecture configuration')
parser.add_argument('--teacher', type=str, metavar='FILE',
                    help='path to teacher model checkpoint FILE')
####BN-MOD
parser.add_argument('--freeze-bn', action='store_true',
                    help='student model will not change batchnorm params (eval mode)')
parser.add_argument('--freeze-bn-running-estimators', action='store_true',
                    help='student model will not change batchnorm params (reload old values before eval) overwrites --freeze-bn')
parser.add_argument('--absorb-bn-step', default=None, type=int,
                    help='limit training steps')
parser.add_argument('--absorb-bn', action='store_true',
                    help='student model absorbs batchnorm before distillation')
parser.add_argument('--fresh-bn', action='store_true',
                    help='student model absorbs batchnorm running mean and var before distillation but leaves bn layers with affine parameters')
parser.add_argument('--otf', action='store_true',
                    help='use on the fly absorbing batchnorm layers')
parser.add_argument('--reset-weights', action='store_true',
                    help='do not load teacher weights to the student model')
parser.add_argument('--no-quantize', action='store_true',
                    help='do not quantize student model')
###DATA
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 8)')
parser.add_argument('--dataset', metavar='DATASET', default='imagenet',
                    help='dataset name or folder')
parser.add_argument('--input_size', type=int, default=None,
                    help='image input size')
parser.add_argument('--dist-set-size', default=None, type=int,
                    help='limit number of examples per class for distilation training (default: None, use entire ds)')
parser.add_argument('--calibration-set-size', default=500, type=int,
                    help='limit number of examples per class for calibration (default: 500, use entire ds)')
parser.add_argument('--shuffle-calibration-steps', default=200, type=int,
                    help='number of calibration steps')
parser.add_argument('--recalibrate', action='store_true',
                    help='use training examples mixup')
parser.add_argument('--distill-aug', nargs='+', type=str,help='use intermediate layer loss',choices=['cutout','ghost','normal'],default=None)
parser.add_argument('--mixup', action='store_true',
                    help='use training examples mixup')
parser.add_argument('--mixup_rate', default=0.5,
                    help='mixup distribution parameter')
parser.add_argument('--mix-target', action='store_true',
                    help='use target mixup')
###OPT
parser.add_argument('--epochs', default=60, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--steps-limit', default=None, type=int,
                    help='limit training steps')
parser.add_argument('--regime', default='default', type=str,
                    help='default regime name')
parser.add_argument('--lr', default=1e-1, type=float,
                    help='default regime base learning rate')
parser.add_argument('--steps-per-epoch', default=-1, type=int,
                    help='number of steps per epoch, value greater than 0'
                         ' will cause training iterator to sample with replacement')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N', help='mini-batch size (default: 256)')
parser.add_argument('-c', '--batch-chunks', default=1, type=int,
                    metavar='N', help='split mini-batch to N chunks (default: 1)')
parser.add_argument('-r','--overwrite-regime',default=None, help='rewrite regime with external list of dicts "[{},{}...]"')
####MISC
parser.add_argument('--pretrain', action='store_true',
                    help='preform layerwise pretraining session before full network distillation')
parser.add_argument('--train-first-conv', action='store_true',
                    help='allow first conv to train')
parser.add_argument('--use-learned-temperature', action='store_true',
                    help='use trainable temperature parameter')
parser.add_argument('--fixed-distillation-temperature', default=1.,type=float,
                    help='use trainable temperature parameter')

####Quant
parser.add_argument('--q-method', default='avg',choices=QuantMeasure._QMEASURE_SUPPORTED_METHODS,
                    help='which quantization method to use')
parser.add_argument('--calibration-resample', action='store_true',
                    help='resample calibration dataset examples')
parser.add_argument('--quant-freeze-steps', default=None, type=int,
                    help='number of steps untill releasing qparams')
parser.add_argument('--free-w-range', action='store_true',
                    help='do not freeze weight dynamic range during training')
parser.add_argument('--quant-once', action='store_true',
                    help='debug regime mode, model params are quantized only once before first iteration the rest of the compute is float')
####Loss
parser.add_argument('--order-weighted-loss', action='store_true',
                    help='loss is proportional to the teacher ordering')
parser.add_argument('--ranking-loss', action='store_true',
                    help='use top1 ranking loss')
parser.add_argument('--aux', choices=['mse','kld','cos','smoothl1'],default=None,
                    help='use intermediate layer loss')
parser.add_argument('--kd-loss', default='',choices=['mse','kld','smoothl1'],
                    help='specify main loss criterion')
parser.add_argument('--aux-loss-scale',default=1.0,type=float,
                    help='overwrite aux loss scale')
parser.add_argument('--loss-scale',default=1.0,type=float,
                    help='overwrite loss scale')
parser.add_argument('--ce-only',action='store_true',
                    help='train with lable cross entropy only')
parser.add_argument('--ce',action='store_true',
                    help='train with lable cross entropy')
parser.add_argument('--uniform-aux-depth-scale',action='store_true',
                    help='do not scale aux loss according to depth')
####Distributed
parser.add_argument('--world-size', default=-1, type=int,
                    help='number of distributed processes')
parser.add_argument('--local_rank', default=-1, type=int,
                    help='rank of distributed processes')
parser.add_argument('--dist-init', default='env://', type=str,
                    help='init used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')

def set_default_regime(model,lr=1e-3,momentum=0.9,dampning=0.1,warmup=(5,1e-8,'cos'),drops=[(40,1,'linear'),(15,1e-1,'cos'),(10,1e-1,'cos')],
                       steps_per_epoch=400,epochs=80,weight_decay=None):
    from utils.regime import cosine_anneal_lr,linear_lr,exp_decay_lr
    def _pop_if_list(l):
        if type(l)==list:
            v=l.pop(0)
        else:
            v=l
        return v

    def _build_phase(lr_start,epoch_start,lr_end,epoch_end,steps_per_epoch,mode='linear',ndrops=None,**kwargs):
        step_start=epoch_start * steps_per_epoch
        regime_phase={'step': step_start}
        regime_phase.update(kwargs)
        if lr_start-lr_end==0:
            regime_phase.update({'lr':lr_start})
        else:
            step_end = epoch_end * steps_per_epoch
            if mode=='cos':
                regime_phase.update(
                    {'step_lambda': cosine_anneal_lr(lr_start, lr_end, step_start, step_end, ndrops)})
            elif mode=='linear':
                regime_phase.update(
                    {'step_lambda': linear_lr(lr_start, lr_end, step_start,step_end)})
            else:
                assert 0,"mode not supported choose from [\'linear\',\'cos\']"
        return regime_phase

    model.regime_epochs = epochs
    model.regime_steps_per_epoch = steps_per_epoch
    #start epoch,epochs,lr modifier
    #cos_drops=[(15,1),(40,1e-2)]
    ## reference settings to fix training regime length in update steps
    model.quant_freeze_steps = -1
    model.absorb_bn_step = -1
    model.regime = []
    lr_start = lr
    epoch_start=0
    # todo regime construction can be reduced to a single loop over num_epochs,lr_scales tuples
    if warmup:
        ramp_up_epochs, warmup_scale,mode = warmup
        model.regime += [_build_phase(lr_start*warmup_scale,epoch_start,
                                      lr_start,ramp_up_epochs,
                                      model.regime_steps_per_epoch,
                                      mode=mode,
                                      optimizer='SGD',
                                      momentum=_pop_if_list(momentum),
                                      dampning=_pop_if_list(dampning))]
        epoch_start+=ramp_up_epochs
    #set drops
    for epochs,lr_md,mode in drops:
        lr_end=lr_start*lr_md
        epoch_end=epoch_start+epochs
        model.regime +=[_build_phase(lr_start,epoch_start,
                                     lr_end,epoch_end,
                                     model.regime_steps_per_epoch,
                                     mode=mode,
                                     optimizer='SGD',
                                     momentum=_pop_if_list(momentum),
                                     dampning=_pop_if_list(dampning))]
        lr_start=lr_end
        epoch_start=epoch_end
    # attach final phase
    model.regime+=[_build_phase(lr_start,epoch_start,
                                lr_start,None,
                                model.regime_steps_per_epoch,
                                optimizer='SGD',
                                momentum=_pop_if_list(momentum),
                                dampning=_pop_if_list(dampning))]
    if weight_decay is not None:
        model.regime[0]['weight_decay']=weight_decay


def freeze_dropout(model):
    for m in model.modules():
        if isinstance(m, torch.nn.Dropout) or isinstance(m, torch.nn.Dropout2d):
            print(m)
            m.eval()

def main():
    global args, best_prec1, dtype
    best_prec1 = 0
    best_val_loss = 9999.9
    args = parser.parse_args()
    if args.args_from_file:
        import json
        with open(args.args_from_file,'r') as f :
            args_l=json.load(f)
            for key,value in args_l.items():
                if key in ['save','device_id']:
                    continue
                assert key in args, f'loaded argument {key} does not exist in current version'
                setattr(args,key,value)
            print(args)

    if args.dataset.startswith('random-') and not args.freeze_bn_running_estimators:
        args.freeze_bn=True
        print('freeze bn layers for random dataset')
    else:
        args.freeze_bn=False

    dtype = torch_dtypes.get(args.dtype)
    torch.manual_seed(args.seed)
    time_stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    distributed = args.local_rank >= 0 or args.world_size > 1
    is_not_master = distributed and args.local_rank > 0
    if distributed:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_init,
                                world_size=args.world_size, rank=args.local_rank)
        args.local_rank = dist.get_rank()
        args.world_size = dist.get_world_size()
        if args.dist_backend == 'mpi':
            # If using MPI, select all visible devices
            args.device_ids = list(range(torch.cuda.device_count()))
        else:
            args.device_ids = [args.local_rank]

    # create model config
    logging.info("creating model %s", args.model)
    model_builder = models.__dict__.get(args.model,getattr(tvmodels,args.model))
    if hasattr(tvmodels,args.model):
        from_model_zoo=True
    else:
        from_model_zoo=False
    train_dataset_name=args.dataset
    val_dataset_name=args.dataset
    model_ds_config = args.dataset
    if args.dataset.startswith('crossover'):
        _ , model_ds_config, train_dataset_name = args.dataset.split('@')
        val_dataset_name=model_ds_config
    elif args.dataset in ['imaginet', 'randomnet'] or 'imagenet' in args.dataset:
        model_ds_config='imagenet'
    elif 'cifar100' in args.dataset:
        model_ds_config='cifar100'
    elif 'cifar10' in args.dataset:
        model_ds_config='cifar10'

    if args.model_config is not '':
        model_config = literal_eval(args.model_config)
    else:
        model_config = {}

    if from_model_zoo:
        quantize_settings = model_config.pop('quantize', {})
    else:
        model_config.update({'input_size': args.input_size, 'dataset':  model_ds_config})
        quantize_settings = model_config
    if args.evaluate:
        args.results_dir = '/tmp'

    if args.save is '':
        ## quantization configuration
        # get first and last layer configuration strings
        conv1,fc = '', '',
        if model_config.get('conv1'):
            conv1 = '_conv1_{}'.format(model_config['conv1'].__str__().strip('\{\}').replace('\'','').replace(': ','').replace(', ',''))
        if model_config.get('fc'):
            fc = '_fc_{}'.format(model_config['fc'].__str__().strip('\{\}').replace('\'','').replace(': ','').replace(', ',''))
        fl_layers = conv1 + fc
        qcfg_g=quantize_settings.get('cfg_groups', {})
        for qcfg_n,cfg in qcfg_g.items():
            if qcfg_n.startswith('preset-'):
                qcfg_n=qcfg_n[7:]+f'_{cfg}'
            fl_layers+='_'+qcfg_n
        if args.freeze_bn_running_estimators:
            bn_mod_tag = '_freeze_bn_estimators'
        elif args.freeze_bn:
            bn_mod_tag = '_freeze_bn_eval'
        elif args.otf:
            bn_mod_tag='_OTF'
        elif args.absorb_bn:
            bn_mod_tag='_absorb_bn'
        elif args.fresh_bn:
            bn_mod_tag= '_fresh_bn'
        else:
            bn_mod_tag='_bn'

        # save all experiments with same setup in the same root dir including calibration checkpoint
        args.save = '{net}{spec}_{gw}w{ga}a{fl}{bn_mod}'.format(
            net=args.model,spec=model_config.get('depth',''),
            gw=quantize_settings.get('weights_numbits','f32'),
            ga=quantize_settings.get('activations_numbits','f32'),
            fl=fl_layers,
            bn_mod=bn_mod_tag)
        # specific optimizations are stored per experiment
        if args.overwrite_regime:
            regime_name = 'custom'
            regime = literal_eval(args.overwrite_regime)
        else:
            regime_name = model_config.get('regime') or args.regime
            regime = None

        opt = '_' + regime_name + f'_lr_{args.lr}'

        if args.kd_loss!='' and not args.ce_only:
            opt += f'_kd-loss-{args.kd_loss}'

        if args.loss_scale != 1.:
            opt += f'_loss_scale_{args.loss_scale}'
        if args.ce or args.ce_only:
            opt += '_ce{}'.format('_only' if args.ce_only else '')

        if args.pretrain:
            opt += '_pretrain'
        if args.aux:
            opt += f'_aux-{args.aux}'
            if args.uniform_aux_depth_scale:
                opt+= '_uniform'
            else:
                opt+='_scaled'
        if args.mixup:
            opt += '_mixup'
            if args.mix_target:
                opt += '_w_targets'
        if args.order_weighted_loss:
            opt+='_order_scale'
        if args.ranking_loss:
            opt+='_ranking_loss'
        if args.quant_once:
            opt += '_float_opt'
        elif args.quant_freeze_steps and args.free_w_range:
            opt += '_free_weights'
        if args.use_learned_temperature or args.fixed_distillation_temperature != 1.:
            opt += '_tau_{}'.format('learned' if args.use_learned_temperature else args.fixed_distillation_temperature )
        if args.dist_set_size:
            opt += f'_cls_lim_{args.dist_set_size}'

    #args.results_dir = os.path.join(os.environ['HOME'],'experiment_results','quantized.pytorch.results')

    save_calibrated_path = os.path.join(args.results_dir,'distiller',args.dataset,args.save)
    if args.exp_group:
        if args.exp_group.startswith('abs@'):
            exp_group_path=args.exp_group[4:]
        else:
            exp_group_path = os.path.join(save_calibrated_path,args.exp_group)

    save_path = os.path.join(save_calibrated_path, time_stamp + opt)
    if not os.path.exists(save_path) and not is_not_master:
        os.makedirs(save_path)

    setup_logging(os.path.join(save_path, 'log.txt'),
                  resume=args.resume is not '',
                  dummy=is_not_master)

    results_path = os.path.join(save_path, 'results')
    if not is_not_master:
        results = ResultsLog(
            results_path, title='Training Results - %s' % opt,resume=args.resume,params=args)

    logging.info("saving to %s", save_path)
    logging.debug("run arguments: %s", args)

    if 'cuda' in args.device and torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
        torch.cuda.set_device(args.device_ids[0])
        cudnn.benchmark = True
    else:
        args.device_ids = None

    logging.info('rewriting calibration resample flag to True')
    args.calibration_resample = True
    teacher = model_builder(**model_config)
    student_model_config=model_config.copy()
    logging.info("created teacher with configuration: %s", model_config)
    logging.debug(teacher)
    if args.teacher is not None:
        logging.info(f"loading teacher checkpoint {args.teacher}")
        teacher_checkpoint = torch.load(args.teacher,map_location='cpu')
        if 'state_dict' in teacher_checkpoint:
            logging.info(f"reference top 1 score {teacher_checkpoint['best_prec1']}")
            teacher_checkpoint = teacher_checkpoint['state_dict']
        teacher.load_state_dict(teacher_checkpoint)
    else:
        teacher_checkpoint = teacher.state_dict()

    if not args.no_quantize:
        if not from_model_zoo:
            # legacy
            ## todo: seperate models that have builtin (hard-coded) quantization from other models
            student_model_config.update({'quantize': True})
            quantize_settings = student_model_config

        if 'weights_numbits' not in quantize_settings:
            quantize_settings.update({'weights_numbits': _DEFUALT_W_NBITS})
        if 'activations_numbits' not in quantize_settings:
            quantize_settings.update({'activations_numbits': _DEFUALT_A_NBITS})

        if args.absorb_bn or args.otf or args.fresh_bn:
            assert not (args.absorb_bn and args.otf or args.absorb_bn and args.fresh_bn or args.otf and args.fresh_bn)
            logging.info('absorbing teacher batch normalization')
            search_absorbe_bn(teacher, verbose=True, remove_bn=not args.fresh_bn, keep_modifiers=args.fresh_bn)
            if not args.fresh_bn:
                if from_model_zoo:
                    remove_bn_writer=QReWriter(remove_bn=True)
                    remove_bn_writer.group_fns={'remove_bn':remove_bn_writer.group_fns['remove_bn']}
                    remove_bn_writer(teacher)
                    quantize_settings.update({'remove_bn':True})
                else:
                    model_config.update({'absorb_bn': True})
                    quantize_settings.update({'absorb_bn': True})
                    teacher_nobn = model_builder(**model_config)
                    teacher_nobn.load_state_dict(teacher.state_dict())
                    teacher = teacher_nobn

            quantize_settings.update({'OTF': args.otf})
    teacher.eval()
    teacher.to(args.device, dtype)
    logging.info("creating apprentice model with configuration: %s", student_model_config)
    model = model_builder(**student_model_config)
    # add bias terms to the student model since we partially/entirely absorbed them in the teacher
    if args.fresh_bn or (args.absorb_bn and from_model_zoo):
        search_absorbe_bn(model, remove_bn=not args.freeze_bn)
    if not args.no_quantize and from_model_zoo:
        #apply quantization on arbitrary models
        rewriter = QReWriter(verbose=1,**quantize_settings)
        rewriter(model)
    logging.debug(model)

    if from_model_zoo:
        if regime_name == 'default':
            set_default_regime(model,args.lr)
        elif regime_name == 'linear':
            set_default_regime(model, args.lr,warmup=(5,1e-3),cos_drops=[])
        elif regime_name == 'short':
            set_default_regime(model, args.lr, warmup=(1,1e-5), cos_drops=[(40,1),(1,1e-1)])

    regime = regime or getattr(model, 'regime', [{'epoch': 0, 'optimizer': 'SGD', 'lr': 0.1,
                                                  'momentum': 0.9,'weight_decay': 1e-4}])
    if args.absorb_bn and not args.otf:
        logging.info('freezing remaining batch normalization in student model')
        set_bn_is_train(model,False,logger=logging)
    logging.info(f'overwriting quantization method with {args.q_method}')
    set_global_quantization_method(model,args.q_method)
    num_parameters = sum([l.nelement() for l in model.parameters()])
    logging.info("number of parameters: %d", num_parameters)

    # Data loading code
    # todo mharoush: add distillation specific transforms
    default_transform = {
        'train': get_transform(val_dataset_name,
                               input_size=args.input_size, augment=True),
        'eval': get_transform(val_dataset_name,
                              input_size=args.input_size, augment=False)
    }
    transform = getattr(model, 'input_transform', default_transform)
    # if args.distill_aug:
    #     trans = transform['train']
    #     if 'normal' in args.distill_aug:
    #         trans = Compose([trans,RandomNoise('normal', 0.05)])
    #     if 'ghost' in args.distill_aug:
    #         trans = Compose([trans, ImgGhosting()])
    #     if 'cutout' in args.distill_aug:
    #         trans = Compose([trans, Cutout()])
    #     if 'mixup' in args.distill_aug:
    #
    transform.update({'train' : Compose([transform['train'], Cutout()])})
    if args.mixup:
        mixer = MixUp()
        mixer.to(args.device)
    else:
        mixer = None
    train_data = get_dataset(train_dataset_name, 'train', transform['train'],limit=args.dist_set_size)
    logging.info(f'train dataset {train_data}')
    if is_not_master and args.steps_per_epoch:
        ## this ensures that all procesees work on the same sampled sub set data but with different samples per batch
        logging.info('setting different seed per worker for random sampler use in distributed mode')
        torch.manual_seed(args.seed * (1+args.local_rank))

    # todo p3:
    #   2. in-batch augmentation
    if args.steps_per_epoch > 0:
        logging.info(f'total steps per epoch {args.steps_per_epoch}')
        logging.info('setting random sampler with replacement for training dataset')
        sampler = torch.utils.data.RandomSampler(train_data,replacement=True,num_samples=args.steps_per_epoch*args.batch_size)
    else:
        sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_data,sampler=sampler,
        batch_size=args.batch_size, shuffle=(sampler is None),
        num_workers=args.workers, pin_memory=not distributed,drop_last=True)

    val_data = get_dataset(val_dataset_name, 'val', transform['eval'])
    val_loader = torch.utils.data.DataLoader(
        val_data,
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=not distributed,drop_last=False)

    repeat = 1
    if args.steps_per_epoch < 0:
        if len(train_loader) / len(val_loader) < 1:
            repeat = len(val_loader) // len(train_loader)

        args.steps_per_epoch=len(train_loader)*repeat
        logging.info(f'total steps per epoch {args.steps_per_epoch}')
    if hasattr(model, 'regime_epochs'):
        args.epochs = model.regime_epochs
    if args.steps_limit:
        args.epochs = min(1+int(args.steps_limit / args.steps_per_epoch),args.epochs)
    logging.info(f'total epochs for training {args.epochs}')

    #pre_train_criterion = nn.MSELoss()
    pre_train_criterion = nn.KLDivLoss(reduction='mean')
    pre_train_criterion.to(args.device)
    loss_scale = args.loss_scale
    aux_loss_scale = args.aux_loss_scale

    if args.kd_loss != '':
        if args.kd_loss=='kld':
            criterion = nn.KLDivLoss(reduction='mean')
        elif args.kd_loss == 'smoothl1':
            criterion = nn.SmoothL1Loss()
        else:
            assert args.kd_loss=='mse'
            criterion = nn.MSELoss()

        criterion.to(args.device, dtype)
    else:
        criterion=None
        assert args.ce_only


    if args.ce or args.ce_only:
        CE = nn.CrossEntropyLoss()
        CE.to(args.device)
    else:
        CE = None

    if args.aux:
        if args.aux == 'kld' :
            aux = nn.KLDivLoss(reduction='mean')
        elif args.aux == 'cos':
            aux = CosineSimilarityChannelWiseLoss()
        elif args.aux == 'mse':
            aux = nn.MSELoss()
        elif args.aux == 'smoothl1':
            aux = nn.SmoothL1Loss()
        aux.to(args.device)

    else:
        aux = None

    if args.order_weighted_loss:
        loss_scale *= 1000

    # define loss function (criterion) and optimizer
    # valid_criterion = getattr(model, 'criterion', nn.CrossEntropyLoss)()
    # valid_criterion.to(args.device,dtype)
    valid_criterion = criterion

    # optionally resume from a checkpoint
    if args.evaluate:
        if not os.path.isfile(args.evaluate):
            parser.error('invalid checkpoint: {}'.format(args.evaluate))
        checkpoint = torch.load(args.evaluate)
        model.load_state_dict(checkpoint['state_dict'])
        logging.info("loaded checkpoint '%s' (epoch %s)",
                     args.evaluate, checkpoint['epoch'])
        validate(val_loader, model, valid_criterion, 0,teacher=None)
        return
    elif args.resume:
        checkpoint_file = args.resume
        if os.path.isdir(checkpoint_file):
            results.load(os.path.join(checkpoint_file, 'results.csv'))
            checkpoint_file = os.path.join(
                checkpoint_file, 'model_best.pth.tar')
        if os.path.isfile(checkpoint_file):
            logging.info("loading checkpoint '%s'", args.resume)
            checkpoint = torch.load(checkpoint_file)
            args.start_epoch = checkpoint['epoch'] - 1
            best_prec1 = checkpoint['best_prec1']
            if args.fresh_bn or (args.absorb_bn and from_model_zoo):
                search_absorbe_bn(model, remove_bn=not args.fresh_bn)
            model.load_state_dict(checkpoint['state_dict'])
            logging.info("loaded checkpoint '%s' (epoch %s)",
                         checkpoint_file, checkpoint['epoch'])
        else:
            logging.error("no checkpoint found at '%s'", args.resume)
            exit(1)

    elif not args.recalibrate and not args.reset_weights and os.path.isfile(os.path.join(save_calibrated_path,'calibrated_checkpoint.pth.tar')):
        student_checkpoint = torch.load(os.path.join(save_calibrated_path,'calibrated_checkpoint.pth.tar'),'cpu')
        logging.info(f"loading pre-calibrated quantized model from {save_calibrated_path}, reported top 1 score {student_checkpoint['best_prec1']}")
        # if args.fresh_bn:
        #     search_absorbe_bn(model,remove_bn=False)
        model.load_state_dict(student_checkpoint['state_dict'],strict=True)
    else:
        # no checkpoint for model, calibrate quant measure nodes and freeze bn
        logging.info("initializing apprentice model with teacher parameters: %s", student_model_config)
        if not args.reset_weights:
            model.load_state_dict(teacher_checkpoint, strict=False)
        #freeze_dropout(model)
        model.to(args.device, dtype)
        model,loss_avg,acc = calibrate(model,train_dataset_name,transform,val_loader=val_loader,logging=logging,resample=args.shuffle_calibration_steps,sample_per_class=args.calibration_set_size)

        student_checkpoint= teacher_checkpoint.copy()
        student_checkpoint.update({'config': student_model_config, 'state_dict': model.state_dict(),
                                   'epoch': 0,'regime':None, 'best_prec1': acc})
        logging.info("saving apprentice checkpoint")
        save_checkpoint(student_checkpoint, path=save_calibrated_path,filename='calibrated_checkpoint')
        if args.recalibrate:
            print(f'reported calibration\t loss-{loss_avg:.3f} top1-{acc:.2f}', )
            if args.exp_group:
                logging.info(f'appending experiment result summary to {args.exp_group} experiment')
                exp_summary = ResultsLog(exp_group_path, title='Result Summary, Experiment Group: %s' % args.exp_group,
                                         resume=1, params=None)
                summary = {'acc_top1': acc, 'loss_avg': loss_avg, 'save_path': save_calibrated_path}
                summary.update(dict(args._get_kwargs()))
                exp_summary.add(**summary)
                exp_summary.save()
            exit(0)

    if args.use_learned_temperature:
        tau=torch.nn.Parameter(torch.ones(1,model.fc.out_features), requires_grad=True)
        model.register_parameter('tau',tau)

    if args.quant_freeze_steps is None:
        args.quant_freeze_steps=getattr(model,'quant_freeze_steps',0)
    if args.quant_freeze_steps>-1:
        logging.info(f'quant params will be released at step {args.quant_freeze_steps}')

    if not args.absorb_bn:
        if args.absorb_bn_step is None:
            args.absorb_bn_step = getattr(model, 'absorb_bn_step', -1)
        if args.absorb_bn_step>-1:
            logging.info(f'bn will be absorbed at step {args.absorb_bn_step}')

    optimizer = OptimRegime(model, regime)
    logging.info('start training with regime-\n'+('{}\n'*len(regime)).format(*[p for p in regime]))
    if args.otf:
        logging.info('updating batch norm learnable modifiers in student model')
        state = model.state_dict().copy()
        teacher_params = []
        for k, v in teacher_checkpoint.items():
            if k not in model.state_dict() and ('weight' in k or 'bias' in k):
                teacher_params.append(v)
        from models.modules.quantize import QuantNode
        if not isinstance(model.conv1,QuantNode):
            teacher_params=teacher_params[2:]
        for k,v in model.state_dict().items():
            if 'bn' in k and ('weight' in k or 'bias' in k):
                state[k] = teacher_params.pop(0)
        model.load_state_dict(state)
    model.to(args.device, dtype)
    if args.pretrain:
        ## layerwise training freeze all previous layers first
        #pretrain(model,teacher,train_loader,optimizer,pre_train_criterion,True,4)
        ## fine tune
        pretrain(model, teacher, train_loader, optimizer, pre_train_criterion,False,3, aux = aux,loss_scale = loss_scale)

    if args.quant_once:
        with torch.no_grad():
            overwrite_params(model,logging)
            set_quant_mode(model,False,logging)
            #set_bn_is_train(model, False, logging)
            pass

    for epoch in range(args.start_epoch , args.epochs):
        ## train for one epoch
        ## absorb bn after absorb bn steps of training
        # if not args.absorb_bn and -1 < args.absorb_bn_step == args.steps_per_epoch*epoch:
        #     logging.info(f'step {epoch*len(train_loader)} absorbing batchnorm layers')
        #     if epoch>0:
        #         #update weights since we are using a master copy in float
        #         overwrite_params(model, logging)
        #     search_absorbe_bn(model)
        #     q_model_config.update({'absorb_bn': True})
        #     no_bn_model = model_builder(**q_model_config)
        #     no_bn_model.load_state_dict(model.state_dict())
        #     model=no_bn_model
        #     model.to(args.device)
        #     if epoch > 0:
        #         model,_,_=calibrate(model,args.dataset,transform,valid_criterion,val_loader=val_loader,logging=logging)
        if args.ce_only:
            train_loss, train_prec1, train_prec5 = train(
                train_loader, model, CE, epoch, optimizer,
                loss_scale=loss_scale, mixer=mixer, quant_freeze_steps=args.quant_freeze_steps,
                dr_weight_freeze=not args.free_w_range, distributed=distributed)
        else:
            if args.freeze_bn_running_estimators:
                logging.info('saving initial bn parameters for all batch normalization')
                set_bn_is_train(model,True,logger=logging,reset_running_estimators=True)
            train_loss, train_prec1, train_prec5 = train(
                train_loader, model, criterion, epoch, optimizer, teacher, aux=aux, ce=CE, loss_scale=loss_scale,
                aux_loss_scale=aux_loss_scale, mixer=mixer, quant_freeze_steps=args.quant_freeze_steps,
                dr_weight_freeze=not args.free_w_range, distributed=distributed,
                aux_depth_scale=not args.uniform_aux_depth_scale)

        if (epoch +1) % repeat == 0 and is_not_master == False:
            # evaluate on validation set
            if args.ce_only:
                val_loss, val_prec1, val_prec5 = validate(
                    val_loader, model, CE, epoch)
            else:
                val_loss, val_prec1, val_prec5 = validate(
                val_loader, model, valid_criterion, epoch,teacher=teacher,
                    loss_scale=loss_scale,distributed=distributed)
            if distributed:
                logging.debug('local rank {} is now saving'.format(args.local_rank))
            timer_save=time.time()
            # remember best prec@1 and save checkpoint
            is_val_best=best_val_loss > val_loss
            if is_val_best:
                best_val_loss=val_loss
                best_loss_epoch=epoch
                best_loss_top1=val_prec1
                best_loss_train=train_loss
            is_best = val_prec1 > best_prec1
            if is_best:
                best_epoch=epoch
                val_best=val_loss
                train_best=train_loss
            best_prec1 = max(val_prec1, best_prec1)
            save_checkpoint({
                'epoch': epoch + 1,
                'model': args.model,
                'config': student_model_config,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
                'regime': regime
            }, is_best, path=save_path,save_freq=args.ckpt_freq)
            logging.info('\n Epoch: {0}\t'
                         'Training Loss {train_loss:.4e} \t'
                         'Training Prec@1 {train_prec1:.3f} \t'
                         'Training Prec@5 {train_prec5:.3f} \t'
                         'Validation Loss {val_loss:.4e} \t'
                         'Validation Prec@1 {val_prec1:.3f} \t'
                         'Validation Prec@5 {val_prec5:.3f} \n'
                         .format(epoch + 1, train_loss=train_loss, val_loss=val_loss,
                                 train_prec1=train_prec1, val_prec1=val_prec1,
                                 train_prec5=train_prec5, val_prec5=val_prec5))

            results.add(epoch=epoch + 1, train_loss=train_loss, val_loss=val_loss,
                        train_error1=100 - train_prec1, val_error1=100 - val_prec1,
                        train_error5=100 - train_prec5, val_error5=100 - val_prec5)
            results.plot(x='epoch', y=['train_loss', 'val_loss'],
                         legend=['training', 'validation'],
                         title='Loss', ylabel='loss')
            results.plot(x='epoch', y=['train_error1', 'val_error1'],
                         legend=['training', 'validation'],
                         title='Error@1', ylabel='error %')
            results.plot(x='epoch', y=['train_error5', 'val_error5'],
                         legend=['training', 'validation'],
                         title='Error@5', ylabel='error %')
            results.save()

        if distributed:
            logging.info(
                'local rank {} done training epoch {}.'.format(args.local_rank,epoch))
            torch.distributed.barrier()
    if is_not_master == False:
        #calc stats for 5 best scores
        scores=results.results['val_error1'].to_numpy()
        scores.sort()
        scores=scores[:5]

        smth_top1_avg,smth_top1_std=scores.mean(),scores.std()
        logging.info(f'Training-Summary:')
        logging.info(f'best-top1:      {best_prec1:.2f}\tval-loss {val_best:.4f}\ttrain-loss {train_best:.4f}\tepoch {best_epoch}')
        logging.info(f'best-loss-top1: {best_loss_top1:.2f}\tval-loss {best_val_loss:.4f}\ttrain-loss {best_loss_train:.4f}\tepoch {best_loss_epoch}')
        logging.info(f'smoothed top1:\tmean {smth_top1_avg:0.4f}\tstd {smth_top1_std:0.4f}')
        logging.info('regime-\n'+('{}\n'*len(regime)).format(*[p for p in regime]))
        save_path=shutil.move(save_path, save_path + f'_top1_{best_prec1:.2f}_loss_{val_best:.4f}_e{best_epoch}')
        logging.info(f'logdir-{save_path}')
        if args.exp_group:
            logging.info(f'appending experiment result summary to {args.exp_group} experiment')
            exp_summary = ResultsLog(exp_group_path, title='Result Summary, Experiment Group: %s' % args.exp_group,
                                     resume=1,params=None)
            summary={'top1_smooth_mean':smth_top1_avg,'top1_smooth_std':smth_top1_std,
                     'best_acc_top1': best_prec1, 'best_acc_val': val_best, 'best_acc_train': train_best,
             'best_acc_epoch': best_epoch,
             'best_loss_top1': best_loss_top1, 'best_loss_val': best_val_loss, 'best_loss_train': best_loss_train,
             'best_loss_epoch': best_loss_epoch,'save_path':save_path}
            summary.update(dict(args._get_kwargs()))
            exp_summary.add(**summary)
            exp_summary.save()
            # results.plot(x='epoch', y=['train_loss', 'val_loss'],
            #              legend=['training', 'validation'],
            #              title='Loss', ylabel='loss')

        os.popen(f'firefox {save_path}/results.html &')
    if distributed:
        logging.info(
            'local rank {} is done, waiting to exit'.format(args.local_rank))
        torch.distributed.barrier()

def forward(data_loader, model, criterion, epoch=0, training=True, optimizer=None,teacher=None,aux=None,ce=None,
            aux_start=0,loss_scale = 1.0,aux_loss_scale=1.0,quant_freeze_steps=0,mixer=None,distributed=False,
            aux_depth_scale=True):
    if aux:
        model = SubModules(model)
        teacher = SubModules(teacher) if teacher else None

    modules = model._modules
    if distributed:
        model = nn.parallel.DistributedDataParallel(model,
                                                     device_ids=args.device_ids,
                                                     output_device=args.device_ids[0])
        teacher = nn.parallel.DistributedDataParallel(teacher,
                                                     device_ids=args.device_ids,
                                                     output_device=args.device_ids[0]) if teacher else None
        mixer = nn.parallel.DistributedDataParallel(mixer,
                                                     device_ids=args.device_ids,
                                                     output_device=args.device_ids[0]) if mixer else None
    elif args.device_ids and len(args.device_ids) > 1 and not isinstance(model,nn.DataParallel):
        #aux = torch.nn.DataParallel(aux) if aux else None
        model = torch.nn.DataParallel(model, args.device_ids)
        teacher = torch.nn.DataParallel(teacher, args.device_ids) if teacher else None
        mixer = torch.nn.DataParallel(mixer, args.device_ids) if mixer else None

    if aux:
        # print('trainable params')
        for r,(k, m) in enumerate(modules.items()):
            for n, p in m.named_parameters():
                if p.requires_grad:
                    # print(f'{k}.{n} shape {p.shape}')
                    if aux_start == -1 and not is_bn(m):
                        logging.debug(f'aux loss will start at {r} for module {k} output')
                        aux_start = r

    regularizer = getattr(model, 'regularization', None)
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    aux_loss_mtr = AverageMeter()
    ranking_loss_mtr = AverageMeter()

    end = time.time()
    _once = 1
    if hasattr(data_loader.sampler,'num_samples'):
        steps_per_epoch = data_loader.sampler.num_samples//data_loader.batch_size
    else:
        steps_per_epoch = len(data_loader)

    for i, (inp, lab) in enumerate(data_loader):
        if training:
            steps = epoch * steps_per_epoch + i
            if -1 < quant_freeze_steps < steps and _once:
                logging.info('releasing model quant parameters')
                freeze_quant_params(model, freeze=False, include_param_dyn_range=True, momentum=0.9999, logger=logging)
                _once = 0
        # measure data loading time
        data_time.update(time.time() - end)
        inp = inp.to(args.device, dtype=dtype)
        lab = lab.to(args.device)
        for c, (inputs, labels) in enumerate(zip(inp.chunk(args.batch_chunks),lab.chunk(args.batch_chunks))):
            aux_loss = torch.tensor(0.).to(args.device)
            if mixer:
                with torch.no_grad():
                    inputs = mixer(inputs,[args.mixup_rate,inputs.size(0),True])
            output_ = model(inputs)

            if teacher:
                with torch.no_grad():
                    target_ = teacher(inputs)
                if aux:
                    aux_outputs, aux_targets=output_[aux_start:-1],target_[aux_start:-1]

                    for k,(output__,target__) in enumerate(zip(aux_outputs,aux_targets)):
                        if isinstance(aux,nn.KLDivLoss) or isinstance(aux,nn.DataParallel) and isinstance(aux._modules['module'],nn.KLDivLoss):
                            with torch.no_grad():
                                ## divide by temp factor to increase entropy todo register as model learnable param
                                a_t = F.softmax(target__,-1)
                            a_o = F.log_softmax(output__,-1)
                        else:
                            a_o = output__
                            a_t = target__

                        if aux_depth_scale:
                            num_outputs_for_aux = (len(output_) - aux_start - 1)
                            depth_scale=2*(k-aux_start +1)/(num_outputs_for_aux**2+num_outputs_for_aux)
                        else:
                            depth_scale=1.0

                        aux_loss += aux(a_o, a_t)*depth_scale

                    aux_loss_mtr.update(float(aux_loss),inputs.size(0))
                    #keep last module output for final loss
                    output_ = output_[-1]
                    target_ = target_[-1]


                if args.use_learned_temperature:
                    assert hasattr(model,'tau')
                    target_ /= model.tau
                    output_ /= model.tau
                else:
                    target_ /= args.fixed_distillation_temperature
                    output_ /= args.fixed_distillation_temperature

                if mixer and args.mix_target:
                    target_ = mixer(target=target_)

                ## normal distillation extract target
                if isinstance(criterion, nn.KLDivLoss):
                    with torch.no_grad():
                        target = F.softmax(target_, -1)
                    output = F.log_softmax(output_, -1)
                else:
                    target = target_
                    output = output_
            else:
                ## use real labels as targets
                target = labels
                output = output_

            if mixer and args.mix_target:
                with torch.no_grad():
                    target = mixer.mix_target(target)

            if args.order_weighted_loss and training:
                with torch.no_grad():
                    target,ids = torch.sort(target,descending=True)
                    ids_ = torch.cat([s + k * target.size(1) for k, s in enumerate(ids)])
                output_flat = output.flatten()
                output = output_flat[ids_].reshape((target.size(0),target.size(1)))
                # using 1 / ni**2 scaling where ni is the ranking of the element i
                # normalization with pi**2 / 6
                with torch.no_grad():
                    #normalizing_sorting_scale=torch.sqrt(0.607927/(torch.arange(1,1001).to(target.device)**2).float()).unsqueeze(0)
                    normalizing_sorting_scale = torch.sqrt(
                      1 / (torch.arange(1, 1001,dtype=torch.float)* torch.log(torch.tensor([target.size(1)],dtype=torch.float)))

                    ).unsqueeze(0).to(target.device)
                    target = torch.mul(target,normalizing_sorting_scale)
                output = torch.mul(output,normalizing_sorting_scale)

            loss = aux_loss*aux_loss_scale + criterion(output, target) * loss_scale

            if ce:
                loss = loss + ce(output,labels)

            if args.ranking_loss and training:
                topk=5
                with torch.no_grad():
                    _,ids = torch.sort(target,descending=True)
                output_flat = output.flatten()
                x1,x2=None,None
                for k in range(topk):
                    with torch.no_grad():
                        ids_top1= ids[:,k:k+1]
                        ids_rest= ids[:,k+1:]
                        #calculate flat ids for slicing
                        ids_top1_= torch.cat([s + r * target.size(1) for r, s in enumerate(ids_top1)])
                        ids_rest_ = torch.cat([s + r * target.size(1) for r, s in enumerate(ids_rest)])
                    if x1 is None:
                        x1 = output_flat[ids_top1_].unsqueeze(1).repeat(1,target.size(1)-1)
                        x2 = output_flat[ids_rest_].reshape((target.size(0),-1))
                    else:
                        x1 = torch.cat(x1,output_flat[ids_top1_].unsqueeze(1).repeat(1, target.size(1) - k - 1))
                        x2 = torch.cat(x2,output_flat[ids_rest_].reshape((target.size(0), -1)))
                gt = torch.ones_like(x2)
                ranking_loss = nn.MarginRankingLoss()(x1,x2,gt)
                ranking_loss_mtr.update(float(ranking_loss),inputs.size(0))
                loss = loss + ranking_loss

            if args.batch_chunks > 1:
                loss = loss / args.batch_chunks

            if regularizer is not None and c==args.batch_chunks-1:
                loss += regularizer(model)
            losses.update(float(loss), inputs.size(0))

            if training:
                if c==0:
                    optimizer.zero_grad()
                ##accumulate gradients
                loss.backward()
            # measure accuracy and record loss
            try:
                prec1, prec5 = accuracy(output.detach(), labels, topk=(1, 5))
                top1.update(prec1.item(), inputs.size(0))
                top5.update(prec5.item(), inputs.size(0))
            except:
                pass

        if i % args.print_freq == 0:
            logging.info('{phase} - Epoch: [{0}][{1}/{2}]  \t{steps}'
                         'Loss {loss.avg:.4e} ({loss.std:.3f}) \t'
                         'Prec@1 {top1.avg:.3f} ({top1.std:.3f}) \t'
                         'Prec@5 {top5.avg:.3f} ({top5.std:.3f})'.format(
                epoch, i, len(data_loader),
                phase='TRAINING' if training else 'EVALUATING',
                steps=f'Train steps: {steps}\t' if training else '',
                loss=losses, top1=top1, top5=top5)+
                         f'\taux_loss {aux_loss_mtr.avg:0.4f}({aux_loss_mtr.std:0.3f})'
                         f'\tranking loss {ranking_loss_mtr.avg:0.4f}({ranking_loss_mtr.std:0.3f})')

        if training:
            #post gradient accumulation step
            optimizer.update(epoch, steps)
            optimizer.step()
        # elif teacher and i == 0:
        #     compare_activations(model,teacher,inputs[:64])

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            logging.info('Time {batch_time.avg:.3f} ({batch_time.std:.3f})\t'
                         'Data {data_time.avg:.3f} ({data_time.std:.3f})\t'.format(batch_time=batch_time,
                                                                                   data_time=data_time))

    return losses.avg, top1.avg, top5.avg

def train(data_loader, model, criterion, epoch, optimizer,teacher=None,aux=None,ce=None,aux_start = 0,loss_scale=1.0,
          aux_loss_scale=1.0,quant_freeze_steps=-1,mixer=None,dr_weight_freeze=True,distributed=False,aux_depth_scale=True):
    # switch to train mode
    model.train()
    if hasattr(data_loader.sampler, 'num_samples'):
        steps_per_epoch = data_loader.sampler.num_samples // data_loader.batch_size
    else:
        steps_per_epoch = len(data_loader)
    if epoch * steps_per_epoch < quant_freeze_steps or quant_freeze_steps==-1:
        logging.info('freezing quant params{}'.format('' if dr_weight_freeze else ' NOT INCL WEIGHTS'))
        freeze_quant_params(model, freeze=True, include_param_dyn_range=dr_weight_freeze, logger=logging)
    if args.freeze_bn:
        logging.info('freezing all batch normalization')
        set_bn_is_train(model,False,logger=logging)

    if teacher:
        if args.absorb_bn and not args.otf:
            logging.info('bn absorbed, freezing remaining batch normalization')
            set_bn_is_train(model,False)

        if not args.train_first_conv:
            if isinstance(model,nn.DataParallel):
                modules_list= list(model._modules['module']._modules.values())
            else:
                modules_list = list(model._modules.values())
            conv_1_module = modules_list[0]
            bn_1_module = modules_list[1]
            if is_bn(bn_1_module):
                bn_1_module.eval()
            # freeze first layer training
            for p in conv_1_module.parameters():
                p.requires_grad = False

    return forward(data_loader, model, criterion, epoch, training=True, optimizer=optimizer, teacher=teacher,
                   aux=aux, ce=ce,aux_start=aux_start,loss_scale=loss_scale, aux_loss_scale=aux_loss_scale,
                   quant_freeze_steps=quant_freeze_steps, mixer=mixer, distributed=distributed,
                   aux_depth_scale=aux_depth_scale)


def validate(data_loader, model, criterion, epoch,teacher=None,loss_scale=1.0,distributed=False):
    # switch to evaluate mode
    if args.freeze_bn_running_estimators:
        logging.info('restoring all batch normalization parameters')
        set_bn_is_train(model,False,logger=logging,reload_running_estimators=True)
    model.eval()
    with torch.no_grad():
        return forward(data_loader, model, criterion, epoch, training=False, optimizer=None,
                       teacher=teacher,loss_scale=loss_scale,distributed=distributed)


def compare_activations(src,tgt,inputs):
    inp = inputs
    with torch.no_grad():
        if isinstance(src,nn.DataParallel):
            src = src._modules['module']
            tgt = tgt._modules['module']
        for tv, sv in zip(src._modules.values(), tgt._modules.values()):
            m_out = sv(inp)
            t_out = tv(inp)
            print((m_out - t_out).abs().mean(), m_out.shape)
            inp = m_out.squeeze()


def pretrain(model,teacher,data,optimizer,criterion,freeze_prev=True,epochs=5,aux=None,loss_scale=10.0):
    logging.info('freezing batchnorms')
    set_bn_is_train(model,False,logging)
    aux = aux if not freeze_prev else None
    mod = nn.Sequential()
    t_mod = nn.Sequential()

    mod.to(args.device)
    t_mod.to(args.device)
    aux_start=0
    defrost_list = []
    for i,(sv, tv) in enumerate(zip(model._modules.values(), teacher._modules.values())):
        # switch to eval mode
        if freeze_prev:
            for m in mod.modules():
                m.eval()
                for p in m.parameters():
                    if p.requires_grad:
                        p.requires_grad = False
                        defrost_list.append(p)

        t_mod.add_module(str(i), tv)
        t_mod.eval()
        mod.add_module(str(i), sv)
        # 'first and last layers stay frozen'
        if i==0 or i == len(model._modules)-1 or is_bn(sv):
            for p in mod.parameters():
                if p.requires_grad:
                    p.requires_grad = False
                    defrost_list.append(p)
            logging.info(f'skipping module {sv}')
            continue
        if all([p.requires_grad == False for p in mod.parameters(True)]) or len([p for p in sv.parameters(True)]) == 0:
            logging.info(f'skipping module {sv}')
            continue
        else:
            logging.info(f'tuning params for module {sv.__str__()}')

        for e in range(epochs):
            train_loss, _ ,_ = train(data, mod, criterion, 0, optimizer=optimizer, teacher=t_mod,aux=aux,aux_start = aux_start,loss_scale=loss_scale)
            logging.info('\nPre-training Module {} - Epoch: {}\tTraining Loss {train_loss:.5f}'.format(i,e + 1, train_loss=train_loss))
    # defrost model
    # model.train()
    for p in defrost_list:
        p.requires_grad = True

#todo replace with forward hook?
# sequential model with intermidiate output collection, usefull when using aux loss and runing data parallel model
class SubModules(nn.Sequential):
    def __init__(self,model):
        super(SubModules,self).__init__(model._modules)

    def forward(self, input):
        output = []
        for module in self._modules.values():
            input = module(input)
            num_parameters = sum([l.nelement() for l in module.parameters()])
            if num_parameters> 0:
                output.append(input)
        return output

def calibrate(model,dataset,transform,calib_criterion=None,resample=200,batch_size=256,workers=4,val_loader=None,sample_per_class=-1,logging=None):
    if logging:
        logging.info("set measure mode")
    # set_bn_is_train(model,False)
    set_measure_mode(model, True, logger=logging)
    if logging:
        logging.info("calibrating model to get quant params")
    calibration_data = get_dataset(dataset, 'train', transform['train'],limit=sample_per_class,shuffle_before_limit=False)
    if logging:
        logging.info(f'calibration dataset {calibration_data}')

    # calibration_data = limitDS(calibration_data, sample_per_class)
    if resample>0:
        cal_sampler = torch.utils.data.RandomSampler(calibration_data, replacement=True,
                                                     num_samples=resample * batch_size)
    else:
        cal_sampler = None

    calibration_loader = torch.utils.data.DataLoader(
        calibration_data, sampler=cal_sampler,
        batch_size=batch_size, shuffle=cal_sampler is None,
        num_workers=workers, pin_memory=False, drop_last=True)
    calib_criterion = calib_criterion or getattr(model, 'criterion', nn.CrossEntropyLoss)()
    calib_criterion.to(args.device,dtype)
    with torch.no_grad():
        losses_avg, top1_avg, top5_avg = forward(calibration_loader, model, calib_criterion, 0, training=False,
                                                 optimizer=None)
    if logging:
        logging.info('Measured float resutls on calibration data:\nLoss {loss:.4f}\t'
                     'Prec@1 {top1:.3f}\t'
                     'Prec@5 {top5:.3f}'.format(loss=losses_avg, top1=top1_avg, top5=top5_avg))
    set_measure_mode(model, False, logger=logging)
    if val_loader:
        if logging:
            logging.info("testing model accuracy")
        losses_avg, top1_avg, top5_avg = validate(val_loader, model, calib_criterion, 0, teacher=None)
        if logging:
            logging.info('Quantized validation results:\nLoss {loss:.4f}\t'
                     'Prec@1 {top1:.3f}\t'
                     'Prec@5 {top5:.3f}'.format(loss=losses_avg, top1=top1_avg, top5=top5_avg))

        return model,losses_avg,top1_avg

    return model
if __name__ == '__main__':
    main()
