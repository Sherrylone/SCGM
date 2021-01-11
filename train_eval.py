import torch.optim as optim
import time
from datetime import datetime
from pathlib import Path
from tensorboardX import SummaryWriter

from src.dataset.data_loader import GMDataset, get_dataloader
from models.GMN.displacement_layer import Displacement
from src.loss_func import *
from src.evaluation_metric import matching_accuracy
from src.parallel import DataParallel
from src.utils.model_sl import load_model, save_model
from eval import eval_model
from src.lap_solvers.hungarian import hungarian
from src.utils.data_to_cuda import data_to_cuda

from src.utils.config import cfg


def train_eval_model(model,
                     criterion,
                     optimizer,
                     dataloader,
                     tfboard_writer,
                     num_epochs=25,
                     resume=False,
                     start_epoch=0):
    print('Start training...')

    since = time.time()
    dataset_size = len(dataloader['train'].dataset)
    displacement = Displacement()
    lap_solver = hungarian

    device = next(model.parameters()).device
    print('model on device: {}'.format(device))

    alphas = torch.tensor(cfg.EVAL.PCK_ALPHAS, dtype=torch.float32, device=device)  # for evaluation

    checkpoint_path = Path(cfg.OUTPUT_PATH) / 'params'
    if not checkpoint_path.exists():
        checkpoint_path.mkdir(parents=True)

    if resume:
        assert start_epoch != 0
        model_path = str(checkpoint_path / 'params_{:04}.pt'.format(start_epoch))
        print('Loading model parameters from {}'.format(model_path))
        load_model(model, model_path)

        optim_path = str(checkpoint_path / 'optim_{:04}.pt'.format(start_epoch))
        print('Loading optimizer state from {}'.format(optim_path))
        optimizer.load_state_dict(torch.load(optim_path))

    scheduler = optim.lr_scheduler.MultiStepLR(optimizer,
                                               milestones=cfg.TRAIN.LR_STEP,
                                               gamma=cfg.TRAIN.LR_DECAY,
                                               last_epoch=cfg.TRAIN.START_EPOCH - 1)

    for epoch in range(start_epoch, num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs - 1))
        print('-' * 10)

        model.train()  # Set model to training mode

        print('lr = ' + ', '.join(['{:.2e}'.format(x['lr']) for x in optimizer.param_groups]))

        epoch_loss = 0.0
        running_loss = 0.0
        running_since = time.time()
        iter_num = 0

        # Iterate over data.
        for inputs in dataloader['train']:
            if model.module.device != torch.device('cpu'):
                inputs = data_to_cuda(inputs)

            iter_num = iter_num + 1

            # zero the parameter gradients
            optimizer.zero_grad()

            with torch.set_grad_enabled(True):
                # forward
                outputs = model(inputs)

                if cfg.PROBLEM.TYPE == '2GM':
                    assert 'ds_mat' in outputs
                    assert 'perm_mat' in outputs
                    assert 'gt_perm_mat' in outputs

                    # compute loss
                    if cfg.TRAIN.LOSS_FUNC == 'offset':
                        d_gt, grad_mask = displacement(outputs['gt_perm_mat'], *outputs['Ps'], outputs['ns'][0])
                        d_pred, _ = displacement(outputs['ds_mat'], *outputs['Ps'], outputs['ns'][0])
                        loss = criterion(d_pred, d_gt, grad_mask)
                    elif cfg.TRAIN.LOSS_FUNC in ['perm', 'ce', 'hung']:
                        loss = criterion(outputs['ds_mat'], outputs['gt_perm_mat'], *outputs['ns'])
                    elif cfg.TRAIN.LOSS_FUNC == 'plain':
                        loss = torch.sum(outputs['loss'])
                    else:
                        raise ValueError('Unsupported loss function {} for problem type {}'.format(cfg.TRAIN.LOSS_FUNC, cfg.PROBLEM.TYPE))

                    # compute accuracy
                    acc, _, __ = matching_accuracy(outputs['perm_mat'], outputs['gt_perm_mat'], outputs['ns'][0])

                elif cfg.PROBLEM.TYPE in ['MGM', 'MGMC']:
                    assert 'ds_mat_list' in outputs
                    assert 'graph_indices' in outputs
                    assert 'perm_mat_list' in outputs
                    assert 'gt_perm_mat_list' in outputs

                    # compute loss & accuracy
                    if cfg.TRAIN.LOSS_FUNC in ['perm', 'ce' 'hung']:
                        loss = torch.zeros(1, device=model.module.device)
                        ns = outputs['ns']
                        for s_pred, x_gt, (idx_src, idx_tgt) in \
                                zip(outputs['ds_mat_list'], outputs['gt_perm_mat_list'], outputs['graph_indices']):
                            l = criterion(s_pred, x_gt, ns[idx_src], ns[idx_tgt])
                            loss += l
                        loss /= len(outputs['ds_mat_list'])
                    elif cfg.TRAIN.LOSS_FUNC == 'plain':
                        loss = torch.sum(outputs['loss'])
                    else:
                        raise ValueError('Unsupported loss function {} for problem type {}'.format(cfg.TRAIN.LOSS_FUNC, cfg.PROBLEM.TYPE))

                    # compute accuracy
                    acc = torch.zeros(1, device=model.module.device)
                    for x_pred, x_gt, (idx_src, idx_tgt) in \
                            zip(outputs['perm_mat_list'], outputs['gt_perm_mat_list'], outputs['graph_indices']):
                        a, _, __ = matching_accuracy(x_pred, x_gt, ns[idx_src])
                        acc += torch.sum(a)
                    acc /= len(outputs['perm_mat_list'])
                else:
                    raise ValueError('Unknown problem type {}'.format(cfg.PROBLEM.TYPE))

                # backward + optimize
                if cfg.FP16:
                    with amp.scale_loss(loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                else:
                    loss.backward()
                optimizer.step()

                batch_num = inputs['batch_size']

                # tfboard writer
                loss_dict = dict()
                loss_dict['loss'] = loss.item()
                tfboard_writer.add_scalars('loss', loss_dict, epoch * cfg.TRAIN.EPOCH_ITERS + iter_num)

                accdict = dict()
                accdict['matching accuracy'] = torch.mean(acc)
                tfboard_writer.add_scalars(
                    'training accuracy',
                    accdict,
                    epoch * cfg.TRAIN.EPOCH_ITERS + iter_num
                )

                # statistics
                running_loss += loss.item() * batch_num
                epoch_loss += loss.item() * batch_num

                if iter_num % cfg.STATISTIC_STEP == 0:
                    running_speed = cfg.STATISTIC_STEP * batch_num / (time.time() - running_since)
                    print('Epoch {:<4} Iteration {:<4} {:>4.2f}sample/s Loss={:<8.4f}'
                          .format(epoch, iter_num, running_speed, running_loss / cfg.STATISTIC_STEP / batch_num))
                    tfboard_writer.add_scalars(
                        'speed',
                        {'speed': running_speed},
                        epoch * cfg.TRAIN.EPOCH_ITERS + iter_num
                    )
                    running_loss = 0.0
                    running_since = time.time()

                tfboard_writer.add_scalars(
                    'learning rate',
                    {'lr_{}'.format(i): x['lr'] for i, x in enumerate(optimizer.param_groups)},
                    epoch * cfg.TRAIN.EPOCH_ITERS + iter_num
                )

        epoch_loss = epoch_loss / dataset_size

        save_model(model, str(checkpoint_path / 'params_{:04}.pt'.format(epoch + 1)))
        torch.save(optimizer.state_dict(), str(checkpoint_path / 'optim_{:04}.pt'.format(epoch + 1)))

        print('Epoch {:<4} Loss: {:.4f}'.format(epoch, epoch_loss))
        print()

        # Eval in each epoch
        accs = eval_model(model, alphas, dataloader['test'])
        acc_dict = {"{}".format(cls): single_acc for cls, single_acc in zip(dataloader['test'].dataset.classes, accs)}
        acc_dict['average'] = torch.mean(accs)
        tfboard_writer.add_scalars(
            'Eval acc',
            acc_dict,
            (epoch + 1) * cfg.TRAIN.EPOCH_ITERS
        )

        scheduler.step()

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}h {:.0f}m {:.0f}s'
          .format(time_elapsed // 3600, (time_elapsed // 60) % 60, time_elapsed % 60))

    return model


if __name__ == '__main__':
    from src.utils.dup_stdout_manager import DupStdoutFileManager
    from src.utils.parse_args import parse_args
    from src.utils.print_easydict import print_easydict

    args = parse_args('Deep learning of graph matching training & evaluation code.')

    import importlib
    mod = importlib.import_module(cfg.MODULE)
    Net = mod.Net

    torch.manual_seed(cfg.RANDOM_SEED)

    dataset_len = {'train': cfg.TRAIN.EPOCH_ITERS * cfg.BATCH_SIZE, 'test': cfg.EVAL.SAMPLES}
    image_dataset = {
        x: GMDataset(cfg.DATASET_FULL_NAME,
                     sets=x,
                     problem=cfg.PROBLEM.TYPE,
                     length=dataset_len[x],
                     cls=cfg.TRAIN.CLASS if x == 'train' else cfg.EVAL.CLASS,
                     obj_resize=cfg.PROBLEM.RESCALE)
        for x in ('train', 'test')}
    dataloader = {x: get_dataloader(image_dataset[x], fix_seed=(x == 'test'))
        for x in ('train', 'test')}

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    model = Net()
    model = model.cuda()

    if cfg.TRAIN.LOSS_FUNC == 'offset':
        criterion = RobustLoss(norm=cfg.TRAIN.RLOSS_NORM)
    elif cfg.TRAIN.LOSS_FUNC == 'perm':
        criterion = PermutationLoss()
    elif cfg.TRAIN.LOSS_FUNC == 'ce':
        criterion = CrossEntropyLoss()
    elif cfg.TRAIN.LOSS_FUNC == 'focal':
        criterion = FocalLoss(alpha=.5, gamma=0.)
    elif cfg.TRAIN.LOSS_FUNC == 'hung':
        criterion = PermutationLossHung()
    elif cfg.TRAIN.LOSS_FUNC == 'hamming':
        criterion = HammingLoss()
    else:
        raise ValueError('Unknown loss function {}'.format(cfg.TRAIN.LOSS_FUNC))

    optimizer = optim.SGD(model.parameters(), lr=cfg.TRAIN.LR, momentum=cfg.TRAIN.MOMENTUM, nesterov=True)

    if cfg.FP16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to enable FP16.")
        model, optimizer = amp.initialize(model, optimizer)

    model = DataParallel(model, device_ids=cfg.GPUS)

    if not Path(cfg.OUTPUT_PATH).exists():
        Path(cfg.OUTPUT_PATH).mkdir(parents=True)

    now_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    tfboardwriter = SummaryWriter(logdir=str(Path(cfg.OUTPUT_PATH) / 'tensorboard' / 'training_{}'.format(now_time)))

    with DupStdoutFileManager(str(Path(cfg.OUTPUT_PATH) / ('train_log_' + now_time + '.log'))) as _:
        print_easydict(cfg)
        model = train_eval_model(model, criterion, optimizer, dataloader, tfboardwriter,
                                 num_epochs=cfg.TRAIN.NUM_EPOCHS,
                                 resume=cfg.TRAIN.START_EPOCH != 0,
                                 start_epoch=cfg.TRAIN.START_EPOCH)
