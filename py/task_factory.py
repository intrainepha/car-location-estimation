# 
import os
import random
import math
import numpy as np
import torch
import torch.distributed as dist
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from glob import glob
from time import time
from tqdm import tqdm
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import List, Callable, Any
from utils.torch_utils import select_device, ModelEMA
from models import Darknet, YOLOLayer, F, parse_data_cfg, \
    attempt_download, load_darknet_weights
from utils.datasets import ImagesAndLabelsLoader
from utils.utils import init_seeds, labels_to_class_weights, labels_to_image_weights, \
    compute_loss, plot_images, plot_results, fitness, check_file, strip_optimizer, \
    print_mutation, plot_evolution_results 
import absl.logging as log
log.set_verbosity(log.INFO)

WEIGHTS_DIR  = 'weights'
LAST         = os.path.join(WEIGHTS_DIR, 'last.pt')
BEST         = os.path.join(WEIGHTS_DIR, 'best.pt')
RES_FILE     = 'results.txt'
HYPER_PARAMS = dict(
    giou         = 3.54,     # giou loss gain
    cls          = 37.4,     # cls loss gain
    cls_pw       = 1.0,      # cls BCELoss positive_weight
    obj          = 64.3,     # obj loss gain (*=img_size/320 if img_size != 320)
    obj_pw       = 1.0,      # obj BCELoss positive_weight
    iou_t        = 0.20,     # iou training threshold
    lr0          = 0.001,    # initial learning rate (SGD=5E-3, Adam=5E-4)
    lrf          = 0.0005,   # final learning rate (with cos scheduler)
    momentum     = 0.937,    # SGD momentum
    weight_decay = 0.01,     # optimizer weight decay
    fl_gamma     = 0.0,      # focal loss gamma (efficientDet default is gamma=1.5)
    hsv_h        = 0.0138,   # image HSV-Hue augmentation (fraction)
    hsv_s        = 0.678,    # image HSV-Saturation augmentation (fraction)
    hsv_v        = 0.36,     # image HSV-Value augmentation (fraction)
    degrees      = 1.98 * 0, # image rotation (+/- deg)
    translate    = 0.05 * 0, # image translation (+/- fraction)
    scale        = 0.05 * 0, # image scale (+/- gain)
    shear        = 0.641 * 0 # image shear (+/- deg)
)  

def get(task:str='train') -> Callable:
    """TODO

    Args:
        TODO

    Raises:
        TODO

    Returns:
        TODO
    """

    pool = dict(
        train  = Trainer,
        test   = Tester,
        detect = Detector
    )
    assert task in pool.keys(), "task {} does not exist !".format(task)

    return pool[task]

@dataclass
class BaseTask(ABC):
    """
    TODO

    Args:
        Same as attributes

    Attributes:
        cfg (str):            *.cfg path
        img_size (int):       [min, max, test]
        weights (str):        initial weights path
        device (str):         device id (i.e. 0 or 0,1 or cpu)
        epochs (int): 
        batch-size (int):
        data (str):           *.data path
        multi_scale (bool):   adjust (67%% - 150%%) img_size every 10 batches
        rect (bool):          rectangular training
        cache_imgs (bool):    cache images for faster training
        weights (str):        initial weights path
        name (str):           renames results.txt to results_name.txt if supplied
        adam (bool):          use adam optimizer
        single_cls (bool):    train as single-class dataset
        freeze_layers (bool): freeze non-output layers
        conf_thres (float):   object confidence threshold
        iou_thres (float):    IOU threshold for NMS
        save_json (bool):     save a cocoapi-compatible JSON results file
        test_mode (str):      'test', 'study', 'benchmark'
        augment (bool):       augmented inference
        names (str):          *.names path'
        source (str):         input file/folder, 0 for webcam
        output (str):         output folder
        fourcc (str):         output video codec (verify ffmpeg support)
        half (bool):          half precision FP16 inference
        view_img (bool):      display results
        save_txt (bool):      save results to *.txt'
        classes (int):        filter by class
        agnostic_nms(bool):   class-agnostic NMS
    """

    cfg: str            = 'cfg/roidepth_0_0_2.cfg'
    img_size: List[int] = field(default_factory=lambda: [128, 128])
    weights: str        = 'weights/roi_net_1_0_0_pre_1000000.weights'
    device: str         = '0'
    epochs: int         = 5
    batch_size: int     = 64
    data: str           = 'data/roidepth-kitti.data'
    multi_scale: bool   = False
    rect: bool          = False
    cache_imgs: bool    = False
    name: str           = ''
    adam: bool          = True
    single_cls: bool    = False
    freeze_layers: bool = False
    conf_thres: float   = 0.001
    iou_thres: float    = 0.35
    save_json: bool     = False
    test_mode: int      = 'test'
    augment: bool       = False
    names: str          = 'data/cls5.names'
    source: str         = 'dataset/kitti/test/images'
    output: str         = 'output'
    fourcc: str         = 'mp4v'
    half: bool          = False
    view_img: bool      = False
    save_txt: bool      = False
    classes: Any        = None
    agnostic_nms: bool  = False

    @abstractmethod
    def run(self):
        """TODO

        Args:
            TODO

        Raises:
            TODO

        Returns:
            TODO
        """

        pass

class Trainer(BaseTask):
    """
    TODO

    Args:
       TODO

    Attributes:
        TODO

    """

    def __call__(self):
        """TODO

        Args:
            TODO

        Raises:
            TODO

        Returns:
            TODO
        """

        log.info(vars(self))
        check_file(self.cfg)
        check_file(self.data)
        self.device = select_device(self.device, batch_size=self.batch_size)
        from torch.utils.tensorboard import SummaryWriter
        log.info('Start Tensorboard with "tensorboard --logdir=runs", view at http://localhost:6006/')
        self.tb_writer = SummaryWriter(comment=self.name)
        self.run() 

    def run(self):
        """TODO

        Args:
            TODO

        Raises:
            TODO

        Returns:
            TODO
        """

        accumulate = max(round(64 / self.batch_size), 1)  # accumulate n times before optimizer update (bs 64)
        weights = self.weights  
        self.extend_img_size()
        imgsz_min, imgsz_max, imgsz_test = self.img_size  
        gs = 32  # (pixels) grid size
        assert math.fmod(imgsz_min, gs) == 0, \
            '--img-size %g must be a %g-multiple' % (imgsz_min, gs)
        self.multi_scale |= imgsz_min != imgsz_max  
        if self.multi_scale:
            if imgsz_min == imgsz_max:
                imgsz_min //= 1.5
                imgsz_max //= 0.667
            grid_min, grid_max = imgsz_min // gs, imgsz_max // gs
            imgsz_min, imgsz_max = int(grid_min * gs), int(grid_max * gs)
        img_size = imgsz_max
        init_seeds()
        data_dict = parse_data_cfg(self.data)
        train_path, test_path = data_dict['train'], data_dict['valid']
        nc = 1 if self.single_cls else int(data_dict['classes'])  
        HYPER_PARAMS['cls'] *= nc / 80  # update coco-tuned HYPER_PARAMS['cls'] to current dataset
        for f in glob('*_batch*.jpg') + glob(RES_FILE):
            os.remove(f)
        model = Darknet(self.cfg).to(self.device)
        pg0, pg1, pg2 = [], [], []  # optimizer parameter groups
        for k, v in dict(model.named_parameters()).items():
            if '.bias' in k:
                pg2 += [v]  # biases
            elif 'Conv2d.weight' in k:
                pg1 += [v]  # apply weight_decay
            else:
                pg0 += [v]  # all else
        if self.adam:
            # HYPER_PARAMS['lr0'] *= 0.1  # reduce lr (i.e. SGD=5E-3, Adam=5E-4)
            optimizer = optim.Adam(pg0, lr=HYPER_PARAMS['lr0'])
            # optimizer = AdaBound(pg0, lr=HYPER_PARAMS['lr0'], final_lr=0.1)
        else:
            optimizer = optim.SGD(
                pg0, lr=HYPER_PARAMS['lr0'], momentum=HYPER_PARAMS['momentum'], nesterov=True
            )
        optimizer.add_param_group({'params': pg1, 'weight_decay': HYPER_PARAMS['weight_decay']})  # add pg1 with weight_decay
        optimizer.add_param_group({'params': pg2})  # add pg2 (biases)
        log.info('Optimizer groups: %g .bias, %g Conv2d.weight, %g other' % (len(pg2), len(pg1), len(pg0)))
        del pg0, pg1, pg2
        start_epoch = 0
        best_fitness = 0.0
        attempt_download(weights)
        if weights.endswith('.pt'):
            ckpt = torch.load(weights, map_location=self.device)
            try:
                ckpt['model'] = {
                    k: v for k, v in ckpt['model'].items()
                    if model.state_dict()[k].numel() == v.numel()
                }
                model.load_state_dict(ckpt['model'], strict=False)
            except KeyError as e:
                s = "%s is not compatible with %s. Specify --weights '' or specify a --cfg compatible with %s. " \
                    "See https://github.com/ultralytics/yolov3/issues/657" % (self.weights, self.cfg, self.weights)
                raise KeyError(s) from e
            # load optimizer
            if ckpt['optimizer'] is not None:
                optimizer.load_state_dict(ckpt['optimizer'])
                best_fitness = ckpt['best_fitness']
            # load results
            if ckpt.get('training_results') is not None:
                with open(RES_FILE, 'w') as file:
                    file.write(ckpt['training_results'])
            # epochs
            start_epoch = ckpt['epoch'] + 1
            if self.epochs < start_epoch:
                log.info('%s has been trained for %g epochs. Fine-tuning for %g additional epochs.' %
                    (self.weights, ckpt['epoch'], self.epochs))
                self.epochs += ckpt['epoch']
            del ckpt
        elif len(weights) > 0: 
            load_darknet_weights(model, weights)
        if self.freeze_layers:
            output_layer_indices = [
                idx - 1 for idx, module in enumerate(model.module_list) if isinstance(module, YOLOLayer)
            ]
            freeze_layer_indices = [
                x for x in range(len(model.module_list)) 
                if (x not in output_layer_indices) and (x - 1 not in output_layer_indices)
            ]
            for idx in freeze_layer_indices:
                for parameter in model.module_list[idx].parameters():
                    parameter.requires_grad_(False)
        lf = lambda x: (((1 + math.cos(x * math.pi / self.epochs)) / 2) ** 1.0) * 0.95 + 0.05  # cosine
        scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)
        scheduler.last_epoch = start_epoch - 1  # see link below
        if self.device.type != 'cpu' and torch.cuda.device_count() > 1 and torch.distributed.is_available():
            dist.init_process_group(
                backend='nccl',                      # 'distributed backend'
                init_method='tcp://127.0.0.1:9999',  # distributed training init method
                world_size=1,                        # number of nodes for distributed training
                rank=0                               # distributed training node rank
            )
            model = torch.nn.parallel.DistributedDataParallel(model, find_unused_parameters=True)
            model.yolo_layers = model.module.yolo_layers  # move yolo layer indices to top level
        # Dataset
        dataset = ImagesAndLabelsLoader(
            train_path, img_size, self.batch_size,
            augment=False,
            hyp=HYPER_PARAMS,              # augmentation hyperparameters
            rect=self.rect,                 # rectangular training
            cache_images=self.cache_imgs,
            single_cls=self.single_cls
        )
        # Dataloader
        self.batch_size = min(self.batch_size, len(dataset))
        nw = min([os.cpu_count(), self.batch_size if self.batch_size > 1 else 0, 8])
        dataloader = torch.utils.data.DataLoader(
            dataset,
            batch_size=self.batch_size,
            num_workers=nw,
            shuffle=not self.rect,
            pin_memory=True,
            collate_fn=dataset.collate_fn
        )
        # Model parameters
        model.nc = nc   # attach number of classes to model
        model.hyp = HYPER_PARAMS # attach hyperparameters to model
        model.gr = 1.0  # giou loss ratio (obj_loss = 1.0 or giou)
        model.class_weights = labels_to_class_weights(dataset.labels, nc).to(self.device)  # attach class weights
        # Model EMA
        ema = ModelEMA(model)
        # Start training
        nb = len(dataloader)  # number of batches
        n_burn = max(3 * nb, 500)  # burn-in iterations, max(3 epochs, 500 iterations)
        maps = np.zeros(nc)  # mAP per class
        # torch.autograd.set_detect_anomaly(True)
        results = (0, 0, 0, 0, 0, 0, 0)  # 'P', 'R', 'mAP', 'F1', 'val GIoU', 'val Objectness', 'val Classification'
        t0 = time()
        log.info('Image sizes {} - {} train, {} test'.format(imgsz_min, imgsz_max, imgsz_test))
        log.info('Using {} dataloader workers'.format(nw))
        log.info('Starting training for {} epochs...'.format(self.epochs))
        for epoch in range(start_epoch, self.epochs):
            model.train()
            # Update image weights (optional)
            if dataset.image_weights:
                w = model.class_weights.cpu().numpy() * (1 - maps) ** 2  # class weights
                image_weights = labels_to_image_weights(dataset.labels, nc=nc, class_weights=w)
                dataset.indices = random.choices(range(dataset.n), weights=image_weights, k=dataset.n)  # rand weighted idx
            mloss = torch.zeros(5).to(self.device)  # mean losses.adaption, original:"mloss = torch.zeros(4).to(device)  # mean losses"
            log.info(
                ('\n' + '%10s' * 9) % 
                ('Epoch', 'gpu_mem', 'GIoU', 'obj', 'cls', 'total', 'depth', 'targets', 'img_size')
            ) # print(('\n' + '%10s' * 8) % ('Epoch', 'gpu_mem', 'GIoU', 'obj', 'cls', 'total', 'targets', 'img_size'))
            pbar = tqdm(enumerate(dataloader), total=nb)  # progress bar
            for i, (imgs, targets, paths, _, roi_info) in pbar:  # batch -------------------------------------------------------------adaption, original:"for i, (imgs, targets, paths, _) in pbar:"
                ni = i + nb * epoch  # number integrated batches (since train start)
                imgs = imgs.to(self.device).float() / 255.0  # uint8 to float32, 0 - 255 to 0.0 - 1.0
                targets = targets.to(self.device)
                # Burn-in
                if ni <= n_burn:
                    xi = [0, n_burn]  # x interp
                    model.gr = np.interp(ni, xi, [0.0, 1.0])  # giou loss ratio (obj_loss = 1.0 or giou)
                    accumulate = max(1, np.interp(ni, xi, [1, 64 / self.batch_size]).round())
                    for j, x in enumerate(optimizer.param_groups):
                        # bias lr falls from 0.1 to lr0, all other lrs rise from 0.0 to lr0
                        x['lr'] = np.interp(ni, xi, [0.1 if j == 2 else 0.0, x['initial_lr'] * lf(epoch)])
                        x['weight_decay'] = np.interp(ni, xi, [0.0, HYPER_PARAMS['weight_decay'] if j == 1 else 0.0])
                        if 'momentum' in x:
                            x['momentum'] = np.interp(ni, xi, [0.9, HYPER_PARAMS['momentum']])
                # Multi-Scale
                if self.multi_scale:
                    if ni / accumulate % 1 == 0:  #  adjust img_size (67% - 150%) every 1 batch
                        img_size = random.randrange(grid_min, grid_max + 1) * gs
                    sf = img_size / max(imgs.shape[2:])  # scale factor
                    if sf != 1:
                        ns = [math.ceil(x * sf / gs) * gs for x in imgs.shape[2:]]  # new shape (stretched to 32-multiple)
                        imgs = F.interpolate(imgs, size=ns, mode='bilinear', align_corners=False)
                # Forward
                pred, pred_depth = model(imgs, roi=roi_info)# adaption, original:"pred = model(imgs)"
                # Loss
                loss, loss_items = compute_loss(pred, pred_depth, targets, model)# adaption, original:"loss, loss_items = compute_loss(pred, targets, model)"
                if not torch.isfinite(loss):
                    log.info('WARNING: non-finite loss, ending training ', loss_items)
                    return results
                # Backward
                loss *= self.batch_size / 64  # scale loss
                loss.backward()
                # Optimize
                if ni % accumulate == 0:
                    optimizer.step()
                    optimizer.zero_grad()
                    ema.update(model)
                # Print
                mloss = (mloss * i + loss_items) / (i + 1)  # update mean losses
                mem = '%.3gG' % (torch.cuda.memory_reserved() / 1E9 if torch.cuda.is_available() else 0)  # (GB)
                s = ('%10s' * 2 + '%10.3g' * 7) % ('%g/%g' % (epoch, self.epochs - 1), mem, *mloss, len(targets), img_size) # adaption, original:"s = ('%10s' * 2 + '%10.3g' * 6) % ('%g/%g' % (epoch, epochs - 1), mem, *mloss, len(targets), img_size)"
                pbar.set_description(s)
                # Plot
                if ni < 1:
                    f = 'train_batch%g.jpg' % i  # filename
                    res = plot_images(images=imgs, targets=targets, paths=paths, fname=f)
                    if self.tb_writer:
                        self.tb_writer.add_image(f, res, dataformats='HWC', global_step=epoch)
                        # tb_writer.add_graph(model, imgs)  # add model to tensorboard
                # end batch ------------------------------------------------------------------------------------------------
            # Update scheduler
            scheduler.step()
            # Process epoch results
            ema.update_attr(model)
            final_epoch = epoch + 1 == self.epochs
            # Write
            with open(RES_FILE, 'a') as f:
                f.write(s + '%10.3g' * 7 % results + '\n')  # P, R, mAP, F1, test_losses=(GIoU, obj, cls)
            # Tensorboard
            if self.tb_writer:
                tags = [
                    'train/giou_loss', 'train/obj_loss',
                    'train/cls_loss', 'metrics/precision',
                    'metrics/recall', 'metrics/mAP_0.5',
                    'metrics/F1', 'val/giou_loss',
                    'val/obj_loss', 'val/cls_loss'
                ]
                for x, tag in zip(list(mloss[:-1]) + list(results), tags):
                    self.tb_writer.add_scalar(tag, x, epoch)
            # Update best mAP
            fi = fitness(np.array(results).reshape(1, -1))  # fitness_i = weighted combination of [P, R, mAP, F1]
            if fi > best_fitness:
                best_fitness = fi
            # Save model
            with open(RES_FILE, 'r') as f:  # create checkpoint
                ckpt = {
                    'epoch': epoch,
                    'best_fitness': best_fitness,
                    'training_results': f.read(),
                    'model': ema.ema.module.state_dict() if hasattr(model, 'module') else ema.ema.state_dict(),
                    'optimizer': None if final_epoch else optimizer.state_dict()
                }
            # Save last, best and delete
            torch.save(ckpt, LAST)
            if (best_fitness == fi) and not final_epoch:
                torch.save(ckpt, BEST)
                log.info('{}-save-as-best'.format(epoch))
            del ckpt
            # end epoch
        # end training
        if len(self.name):
            self.name = '_' + self.name if not self.name.isnumeric() else self.name
            fresults, flast, fbest = \
                'results{}.txt'.format(self.name), \
                WEIGHTS_DIR + 'last{}.pt'.format(self.name), \
                WEIGHTS_DIR + 'best{}.pt'.format(self.name)
            for f1, f2 in zip(
                [WEIGHTS_DIR + 'last.pt', WEIGHTS_DIR + 'best.pt', 'results.txt'],
                [flast, fbest, fresults]
            ):
                if os.path.exists(f1):
                    os.rename(f1, f2)  # rename
                    ispt = f2.endswith('.pt')  # is *.pt
                    strip_optimizer(f2) if ispt else None  # strip optimizer
        plot_results()  # save as results.png
        log.info(
            '%g epochs completed in %.3f hours.\n' % (epoch - start_epoch + 1, (time() - t0) / 3600)
        )
        dist.destroy_process_group() if torch.cuda.device_count() > 1 else None
        torch.cuda.empty_cache()
        return results

    def extend_img_size(self):
        """TODO

        Args:
            TODO

        Raises:
            TODO

        Returns:
            TODO
        """

        # Extend to 3 sizes (min, max, test)
        self.img_size.extend([self.img_size[-1]] * (3 - len(self.img_size)))  
        grid_size = 32
        assert math.fmod(self.img_size[0], grid_size) ==  0, \
            'img_size {} must be a {}-multiple'.format(self.img_size, grid_size)
        self.multi_scale |= self.img_size[0] != self.img_size[1]  
        if self.multi_scale:
            if self.img_size[0] == self.img_size[1]:
                self.img_size[0] //= 1.5
                self.img_size[1] //= 0.667
            grid_min, grid_max = self.img_size[0] // grid_size, self.img_size[1] // grid_size
            self.img_size[0], self.img_size[1] = \
                int(grid_min * grid_size), int(grid_max * grid_size)
        return 

class Tester(BaseTask):
    """
    TODO

    Args:
       TODO

    Attributes:
        TODO

    """

    def run(self):
        """TODO

        Args:
            TODO

        Raises:
            TODO

        Returns:
            TODO
        """


class Detector(BaseTask):
    """
    TODO

    Args:
       TODO

    Attributes:
        TODO

    """

    def run(self):
        """TODO

        Args:
            TODO

        Raises:
            TODO

        Returns:
            TODO
        """

if __name__=="__main__":
    T = get(task="train")
    task = T()
    task()