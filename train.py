# System libs
import os
import time
# import math
import random
import argparse
from distutils.version import LooseVersion
import math
# Numerical libs
import torch
import torch.nn as nn
import torch.utils.data as data
from data.augmentations import Compose, RandomSizedCrop, AdjustContrast, AdjustBrightness, RandomVerticallyFlip, RandomHorizontallyFlip, RandomRotate, PaddingCenterCrop
# Our libs
from data.ac17_dataloader import AC17Data as AC17, AC17_2DLoad as load2D
from models import ModelBuilder, SegmentationModule
from utils import AverageMeter, parse_devices, accuracy, intersectionAndUnion
from lib.nn import UserScatteredDataParallel, async_copy_to,  user_scattered_collate, patch_replication_callback
import lib.utils.data as torchdata
from lib.utils import as_numpy
import numpy as np
from loss import ACLoss
from radam import RAdam

def eval(loader_val, segmentation_module, args, crit):
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    loss_meter = AverageMeter()

    segmentation_module.eval()
    for batch_data in loader_val:
        batch_data = batch_data[0]
        seg_label = as_numpy(batch_data["mask"][0])
        torch.cuda.synchronize()
        batch_data["image"] = batch_data["image"].unsqueeze(0).cuda()
        #batch_data["mask"][0] = batch_data["mask"][0].cuda()
        #batch_data["mask"][1] = batch_data["mask"][1].cuda()

        with torch.no_grad():
            segSize = (seg_label.shape[0], seg_label.shape[1])
            scores = torch.zeros(1, args.num_class, segSize[0], segSize[1])
            scores = async_copy_to(scores, args.gpu)
            feed_dict = batch_data.copy()
            #print(torch.max(feed_dict['image']))   
        
            # forward pass
            scores_tmp, loss = segmentation_module(feed_dict, epoch=0, segSize=segSize)   
            scores = scores + scores_tmp  / len(args.imgSize)
            loss_meter.update(loss)

            _, pred = torch.max(scores, dim=1)
            pred = as_numpy(pred.squeeze(0).cpu())
                #print(np.amax(pred))
        torch.cuda.synchronize()
        # calculate accuracy
        intersection, union = intersectionAndUnion(pred, seg_label, args.num_class)
        intersection_meter.update(intersection)
        union_meter.update(union)
        
    # summary
    iou = intersection_meter.sum / (union_meter.sum + 1e-10)
    for i, _iou in enumerate(iou):
        if i >= 1:
            print('class [{}], IoU: {:.4f}'.format(i, _iou))
    print('loss: {:.4f}'.format(loss_meter.average()))
    return iou[1:], loss_meter.average()
    
# train one epoch
def train(segmentation_module, loader_train, optimizers, history, epoch, args):

    batch_time = AverageMeter()
    data_time = AverageMeter()
    ave_total_loss = AverageMeter()
    ave_acc = AverageMeter()
    ave_j1 = AverageMeter()
    ave_j2 = AverageMeter()
    ave_j3 = AverageMeter()

    segmentation_module.train(not args.fix_bn)
    
    # main loop
    tic = time.time()
    iter_count = 0

    if epoch == args.start_epoch and args.start_epoch > 1:
        scale_running_lr = ((1. - float(epoch-1) / (args.num_epoch)) ** args.lr_pow)
        args.running_lr_encoder = args.lr_encoder * scale_running_lr
        for param_group in optimizers[0].param_groups:
            param_group['lr'] = args.running_lr_encoder
    
    for batch_data in loader_train:
        data_time.update(time.time() - tic)
        batch_data["image"] = batch_data["image"].cuda()
        segmentation_module.zero_grad()
        # forward pass
        loss, acc = segmentation_module(batch_data, epoch)
        loss = loss.mean()
        
        jaccard = acc[1]
        for j in jaccard:
            j = j.float().mean()
        acc = acc[0].float().mean()
        
        # Backward
        loss.backward()
        for optimizer in optimizers:
            optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - tic)
        tic = time.time()
        iter_count += args.batch_size_per_gpu 

        # update average loss and acc
        ave_total_loss.update(loss.data.item())
        ave_acc.update(acc.data.item()*100)

        ave_j1.update(jaccard[0].data.item()*100)
        ave_j2.update(jaccard[1].data.item()*100)
        ave_j3.update(jaccard[2].data.item()*100)
            
        if iter_count % (args.batch_size_per_gpu*10) == 0:
            # calculate accuracy, and display
            if args.unet==False:
                print('Epoch: [{}][{}/{}], Time: {:.2f}, Data: {:.2f}, '
                        'lr_encoder: {:.6f}, lr_decoder: {:.6f}, '
                        'Accuracy: {:4.2f}, Loss: {:.6f}'
                        .format(epoch, i, args.epoch_iters,
                        batch_time.average(), data_time.average(),
                        args.running_lr_encoder, args.running_lr_decoder,
                        ave_acc.average(), ave_total_loss.average()))
            else:
                print('Epoch: [{}/{}], Iter: [{}], Time: {:.2f}, Data: {:.2f},'
                        ' lr_unet: {:.6f}, Accuracy: {:4.2f}, Jaccard: [{:4.2f},{:4.2f},{:4.2f}], ' 
                        'Loss: {:.6f}'
                        .format(epoch, args.max_iters, iter_count,
                            batch_time.average(), data_time.average(),
                            args.running_lr_encoder, ave_acc.average(),
                            ave_j1.average(), ave_j2.average(),
                            ave_j3.average(), ave_total_loss.average()))
    
    #Average jaccard across classes.
    j_avg = (ave_j1.average() + ave_j2.average() + ave_j3.average())/3

    #Update the training history
    history['train']['epoch'].append(epoch)
    history['train']['loss'].append(loss.data.item())
    history['train']['acc'].append(acc.data.item())
    history['train']['jaccard'].append(j_avg)
    # adjust learning rate
    adjust_learning_rate(optimizers, epoch, args)


def checkpoint(nets, history, args, epoch_num):
    print('Saving checkpoints...')
    if args.unet:
        (unet, crit) = nets
    else:
        (net_encoder, net_decoder, crit) = nets
    
    suffix_latest = 'epoch_{}.pth'.format(epoch_num)
    
    torch.save(history,
               '{}/history_{}'.format(args.ckpt, suffix_latest))
    if args.unet:
        dict_unet = unet.state_dict()
        torch.save(dict_unet,
                    '{}/unet_{}'.format(args.ckpt, suffix_latest))
    else:    
        dict_encoder = net_encoder.state_dict()
        dict_decoder = net_decoder.state_dict()

        torch.save(dict_encoder,
                   '{}/encoder_{}'.format(args.ckpt, suffix_latest))
        torch.save(dict_decoder,
                   '{}/decoder_{}'.format(args.ckpt, suffix_latest))

    # dict_encoder_save = {k: v for k, v in dict_encoder.items() if not (k.endswith('_tmp_running_mean') or k.endswith('tmp_running_var'))}
    # dict_decoder_save = {k: v for k, v in dict_decoder.items() if not (k.endswith('_tmp_running_mean') or k.endswith('tmp_running_var'))}


def group_weight(module):
    group_decay = []
    group_no_decay = []
    for m in module.modules():
        if isinstance(m, nn.Linear):
            group_decay.append(m.weight)
            if m.bias is not None:
                group_no_decay.append(m.bias)
        elif isinstance(m, nn.modules.conv._ConvNd):
            group_decay.append(m.weight)
            if m.bias is not None:
                group_no_decay.append(m.bias)
        elif isinstance(m, nn.modules.batchnorm._BatchNorm):
            if m.weight is not None:
                group_no_decay.append(m.weight)
            if m.bias is not None:
                group_no_decay.append(m.bias)

    #ssert len(list(module.parameters())) == len(group_decay) + len(group_no_decay)
    groups = [dict(params=group_decay), dict(params=group_no_decay, weight_decay=.0)]
    return groups


def create_optimizers(nets, args):
    if args.unet == False:
        (net_encoder, net_decoder, crit) = nets
        optimizer_encoder = torch.optim.SGD(
            group_weight(net_encoder),
            lr=args.lr_encoder,
            momentum=args.beta1,
            weight_decay=args.weight_decay)
        optimizer_decoder = torch.optim.SGD(
            group_weight(net_decoder),
            lr=args.lr_decoder,
            momentum=args.beta1,
            weight_decay=args.weight_decay)
        return (optimizer_encoder, optimizer_decoder)
    else:
        (unet, crit) = nets
        '''
        optimizer_unet = torch.optim.Adam(
            group_weight(unet),
            lr = args.lr_encoder,
            betas=(0.9, 0.999))
        optimizer_unet = torch.optim.SGD(
                group_weight(unet),
                lr=args.lr_encoder,
                momentum=args.beta1,
                weight_decay=args.weight_decay,
                nesterov=False)
        '''
        optimizer_unet = RAdam(
                group_weight(unet),
                lr=args.lr_encoder,
                betas=(0.9, 0.999))
        return [optimizer_unet]


def adjust_learning_rate(optimizers, cur_iter, args):
    #if cur_iter <= int(args.num_epoch*0.9):
    #    scale_running_lr = ((1. - float(cur_iter) / (args.num_epoch)) ** args.lr_pow)
    #else:
    #    scale_running_lr = ((1. - args.num_epoch*0.9 / (args.num_epoch)) ** args.lr_pow)
    scale_running_lr = 0.5*(1+math.cos(3.14159*(cur_iter)/args.num_epoch))
    '''
    if cur_iter % 40 != 0:
        return
    p = cur_iter/40
    scale_running_lr = 0.1**p
    '''
    args.running_lr_encoder = args.lr_encoder * scale_running_lr
    args.running_lr_decoder = args.lr_decoder * scale_running_lr
    
    optimizer_unet = optimizers[0]
    for param_group in optimizer_unet.param_groups:
        param_group['lr'] = args.running_lr_encoder

def main(args):
    # Network Builders
    builder = ModelBuilder()
    net_encoder=None
    net_decoder=None
    unet=None
    
    if args.unet == False:
        net_encoder = builder.build_encoder(
            arch=args.arch_encoder,
            fc_dim=args.fc_dim,
            weights=args.weights_encoder)
        net_decoder = builder.build_decoder(
            arch=args.arch_decoder,
            fc_dim=args.fc_dim,
            num_class=args.num_class,
            weights=args.weights_decoder)
    else:
        unet = builder.build_unet(num_class=args.num_class, 
            arch=args.unet_arch,
            weights=args.weights_unet)

        print("Froze the following layers: ")
        for name, p in unet.named_parameters():
            if p.requires_grad == False:
                print(name)
        print()
    
    crit = ACLoss(mode="train")
    #crit = nn.CrossEntropyLoss().cuda()
    #crit = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(50))
    #crit = nn.CrossEntropyLoss().cuda()
    #crit = nn.BCELoss()

    if args.arch_decoder.endswith('deepsup') and args.unet == False:
        segmentation_module = SegmentationModule(
            net_encoder, net_decoder, crit, args.deep_sup_scale)
    else:
        segmentation_module = SegmentationModule(
            net_encoder, net_decoder,  crit, is_unet=args.unet, unet=unet)

    train_augs = Compose([PaddingCenterCrop(256), RandomHorizontallyFlip(), RandomVerticallyFlip(), RandomRotate(180)])
    test_augs = Compose([PaddingCenterCrop(256)])
    # Dataset and Loader
    dataset_train = AC17(
            root=args.data_root,
            split='train',
            k_split=args.k_split,
            augmentations=train_augs,
            img_norm=args.img_norm)
    ac17_train = load2D(dataset_train, split='train', deform=True)
    
    loader_train = data.DataLoader(
        ac17_train,
        batch_size=args.batch_size_per_gpu,  # we have modified data_parallel
        shuffle=True, 
        num_workers=int(args.workers),
        drop_last=True,
        pin_memory=True)
    dataset_val = AC17(
            root=args.data_root,
            split='val',
            k_split=args.k_split,
            augmentations=test_augs,
            img_norm=args.img_norm)
    ac17_val = load2D(dataset_val, split='val', deform=False)
    loader_val = data.DataLoader(
        ac17_val,
        batch_size=1,
        shuffle=False,
        collate_fn=user_scattered_collate,
        num_workers=5,
        drop_last=True)
    # create loader iterator
    #iterator_train = iter(loader_train)

    # load nets into gpu
    if len(args.gpus) > 1:
        segmentation_module = UserScatteredDataParallel(
            segmentation_module,
            device_ids=args.gpus)
        # For sync bn
        patch_replication_callback(segmentation_module)
    segmentation_module.cuda()
    
    # Set up optimizers
    nets = (net_encoder, net_decoder, crit) if args.unet == False else (unet, crit)
    optimizers = create_optimizers(nets, args)

    # Main loop
    history = {'train': {'epoch': [], 'loss': [], 'acc': [], 'jaccard': []}}
    best_val = {'epoch_1': 0, 'mIoU_1': 0,
                'epoch_2': 0, 'mIoU_2': 0,
                'epoch_3': 0, 'mIoU_3': 0,
                'epoch' : 0, 'mIoU': 0}

    for epoch in range(args.start_epoch, args.num_epoch + 1):
        train(segmentation_module, loader_train, optimizers, history, epoch, args)
        iou, loss = eval(loader_val, segmentation_module, args, crit)
        #checkpointing
        ckpted = False
        if loss < 0.215:
            ckpted = True
        if iou[0] > best_val['mIoU_1']:
            best_val['epoch_1'] = epoch
            best_val['mIoU_1'] = iou[0]
            ckpted = True

        if iou[1] > best_val['mIoU_2']:
            best_val['epoch_2'] = epoch
            best_val['mIoU_2'] = iou[1]
            ckpted = True

        if iou[2] > best_val['mIoU_3']:
            best_val['epoch_3'] = epoch
            best_val['mIoU_3'] = iou[2]
            ckpted = True
        
        if (iou[0]+iou[1]+iou[2])/3 > best_val['mIoU']:
            best_val['epoch'] = epoch
            best_val['mIoU'] = (iou[0]+iou[1]+iou[2])/3
            ckpted = True
        
        if epoch % 50 == 0:
            checkpoint(nets, history, args, epoch)
            continue

        if epoch == args.num_epoch:
            checkpoint(nets, history, args, epoch)
            continue
        if epoch < 15:
            ckpted = False
        if ckpted == False:
            continue
        else:
            checkpoint(nets, history, args, epoch)
            continue
        print()
    
    #print("[Val] Class 1: Epoch " + str(best_val['epoch_1']) + " had the best mIoU of " + str(best_val['mIoU_1']) + ".")
    #print("[Val] Class 2: Epoch " + str(best_val['epoch_2']) + " had the best mIoU of " + str(best_val['mIoU_2']) + ".")
    #print("[Val] Class 3: Epoch " + str(best_val['epoch_3']) + " had the best mIoU of " + str(best_val['mIoU_3']) + ".")
    print('Training Done!')


if __name__ == '__main__':
    assert LooseVersion(torch.__version__) >= LooseVersion('0.4.0'), \
        'PyTorch>=0.4.0 is required'
    
    DATA_ROOT = os.getenv('DATA_ROOT', '/home/rexma/Desktop/MRI_Images/AC17')
    DATASET_NAME = "AC17"

    parser = argparse.ArgumentParser()
    # Model related arguments
    parser.add_argument('--id', default='baseline',
                        help="a name for identifying the model")
    parser.add_argument('--unet', default=True,
                        help="use unet?")
    parser.add_argument('--unet_arch', default='albunet',
                        help="UNet architecture")
    parser.add_argument('--arch_encoder', default='resnet50dilated',
                        help="architecture of net_encoder")
    parser.add_argument('--arch_decoder', default='ppm_deepsup',
                        help="architecture of net_decoder")
    parser.add_argument('--weights_encoder', default='/home/rexma/Desktop/seg/encoder_epoch_20.pth',
                        help="weights to finetune net_encoder")
    parser.add_argument('--weights_decoder', default='',
                        help="weights to finetune net_decoder")
    parser.add_argument('--weights_unet', default='',
                        help="weights to finetune unet")
    parser.add_argument('--fc_dim', default=2048, type=int,
                        help='number of features between encoder and decoder')

    # Path related arguments
    parser.add_argument('--data-root', type=str, default=DATA_ROOT)
    parser.add_argument('--img-norm', default=True, action='store_true', help="normalize img value to [0, 1]")

    # optimization related arguments
    parser.add_argument('--gpus', default='0',
                        help='gpus to use, e.g. 0-3 or 0,1,2,3')
    parser.add_argument('--batch_size_per_gpu', default=1, type=int,
                        help='input batch size')
    parser.add_argument('--num_epoch', default=120, type=int,
                        help='epochs to train for')
    parser.add_argument('--start_epoch', default=1, type=int,
                        help='epoch to start training. useful if continue from a checkpoint')
    parser.add_argument('--epoch_iters', default=160, type=int,
                        help='iterations of each epoch (irrelevant to batch size)')
    parser.add_argument('--optim', default='Adam', help='optimizer')
    parser.add_argument('--lr_encoder', default=0.0005, type=float, help='LR')
    parser.add_argument('--lr_decoder', default=0.05, type=float, help='LR')
    parser.add_argument('--lr_pow', default=0.9, type=float,
                        help='power in poly to drop LR')
    parser.add_argument('--beta1', default=0.9, type=float,
                        help='momentum for sgd, beta1 for adam')
    parser.add_argument('--weight_decay', default=1e-4, type=float,
                        help='weights regularizer')
    parser.add_argument('--deep_sup_scale', default=0.4, type=float,
                        help='the weight of deep supervision loss')
    parser.add_argument('--fix_bn', action='store_true',
                        help='fix bn params')

    # Data related argument
    parser.add_argument('--num_class', default=4, type=int,
                        help='number of classes')
    parser.add_argument('--workers', default=1, type=int,
                        help='number of data loading workers')
    parser.add_argument('--dataset-name', type=str, default="AC17")
    parser.add_argument('--k_split', default=1)

    # Misc arguments
    parser.add_argument('--seed', default=304, type=int, help='manual seed')
    parser.add_argument('--ckpt', default='/home/rexma/Desktop/JesseSun/ac17_seg/ckpt',
                        help='folder to output checkpoints')
    parser.add_argument('--disp_iter', type=int, default=20,
                        help='frequency to display')
    


    args = parser.parse_args()
    print("Input arguments:")
    for key, val in vars(args).items():
        print("{:16} {}".format(key, val))

    # Parse gpu ids
    all_gpus = parse_devices(args.gpus)
    all_gpus = [x.replace('gpu', '') for x in all_gpus]
    args.gpus = [int(x) for x in all_gpus]
    num_gpus = len(args.gpus)
    args.batch_size = num_gpus * args.batch_size_per_gpu
    args.gpu = 0

    args.max_iters = args.num_epoch
    args.running_lr_encoder = args.lr_encoder
    args.running_lr_decoder = args.lr_decoder

    args.arch_encoder = args.arch_encoder.lower()
    args.arch_decoder = args.arch_decoder.lower()
        
    args.imgSize = [128]

    # Model ID
    if args.unet ==False:
        args.id += '-' + args.arch_encoder
        args.id += '-' + args.arch_decoder
    else:
        args.id += '-' + str(args.unet_arch)

    args.id += '-ngpus' + str(num_gpus)
    args.id += '-batchSize' + str(args.batch_size) 
    
    if args.unet == False:
        args.id += '-LR_encoder' + str(args.lr_encoder)
        args.id += '-LR_decoder' + str(args.lr_decoder)
    else:
        args.id += '-LR_unet' + str(args.lr_encoder)
        
    args.id += '-epoch' + str(args.num_epoch)
    if args.fix_bn:
        args.id += '-fixBN'
    print('Model ID: {}'.format(args.id))

    # FIRST TIME WE TRAINING IT, LOAD THE PRETRAINED WEIGHTS OF ENCODER FROM IMAGENET.
    #args.weights_encoder = '/home/sunjesse/scratch/seg/encoder_epoch_20.pth'

    args.ckpt = os.path.join(args.ckpt, args.id)
    if not os.path.isdir(args.ckpt):
        os.makedirs(args.ckpt)

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    main(args)
