import torch
import torch.optim as optim
import time
from datetime import datetime
from pathlib import Path
from tensorboardX import SummaryWriter
import scipy.sparse as ssp

from data.data_loader import GMDataset, get_dataloader
from GMN.displacement_layer import Displacement
from GMN.robust_loss import RobustLoss
from utils.evaluation_metric import pck as eval_pck
from parallel import DataParallel
from utils.model_sl import load_model, save_model
from eval import eval_model

from utils.config import cfg


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

    scheduler = optim.lr_scheduler.StepLR(optimizer,
                                          step_size=cfg.TRAIN.LR_STEP,
                                          gamma=cfg.TRAIN.LR_DECAY,
                                          last_epoch=cfg.TRAIN.START_EPOCH - 1)

    for epoch in range(start_epoch, num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs - 1))
        print('-' * 10)

        print('lr = ' + ', '.join(['{:.2e}'.format(x['lr']) for x in optimizer.param_groups]))

        scheduler.step()
        model.train()  # Set model to training mode

        epoch_loss = 0.0
        running_loss = 0.0
        running_since = time.time()
        iter_num = 0

        # Iterate over data.
        for inputs in dataloader['train']:
            img1, img2 = inputs['images']
            P1_gt, P2_gt, P1, P2 = inputs['Ps']
            n1_gt, n2_gt, n1, n2 = inputs['ns']
            e1_gt, e2_gt, e1, e2 = inputs['es']
            G1_gt, G2_gt, G1, G2 = inputs['Gs']
            H1_gt, H2_gt, H1, H2 = inputs['Hs']
            KG, KH = inputs['Ks']
            perm_mat = inputs['gt_perm_mat']

            P1_gt = P1_gt.cuda()
            P2 = P2.cuda()
            n1_gt = n1_gt.cuda()
            perm_mat = perm_mat.cuda()
            P2_gt = P2_gt.cuda()
            KG, KH = KG.cuda(), KH.cuda()

            iter_num = iter_num + img1.size(0)

            # zero the parameter gradients
            optimizer.zero_grad()

            with torch.set_grad_enabled(True):
                # forward
                s_pred, d_pred = model(img1, img2, P1_gt, P2, G1_gt, G2, H1_gt, H2, n1_gt, n2, KG, KH, tfboard_writer)

                d_gt, grad_mask = displacement(perm_mat, P1_gt, P2_gt, n1_gt)
                loss = criterion(d_pred, d_gt, grad_mask)

                # backward + optimize
                loss.backward()
                optimizer.step()

                # training pck statistic
                thres = torch.empty(img1.size(0), len(alphas)).cuda()
                for b in range(img1.size(0)):
                    thres[b] = alphas * cfg.EVAL.PCK_L
                pck, _, __ = eval_pck(P2, P2_gt, s_pred, thres, n1_gt)

                # tfboard writer
                tfboard_writer.add_scalars('loss', {'loss': loss.item()}, epoch * cfg.TRAIN.EPOCH_ITERS + iter_num)
                tfboard_writer.add_scalars(
                    'training accuracy',
                    {'PCK@{:.2f}'.format(a): p for a, p in zip(alphas, pck)},
                    epoch * cfg.TRAIN.EPOCH_ITERS + iter_num
                )

                # statistics
                running_loss += loss.item() * img1.size(0)
                epoch_loss += loss.item() * img1.size(0)

                if iter_num % cfg.STATISTIC_STEP == 0:
                    running_speed = cfg.STATISTIC_STEP / (time.time() - running_since)
                    print('Epoch {:<4} Iteration {:<4} {:>4.2f}sample/s Loss={:<8.4f}'
                          .format(epoch, iter_num, running_speed, running_loss / cfg.STATISTIC_STEP))
                    tfboard_writer.add_scalars(
                        'speed',
                        {'speed': running_speed},
                        epoch * cfg.TRAIN.EPOCH_ITERS + iter_num
                    )
                    running_loss = 0.0
                    running_since = time.time()

        epoch_loss = epoch_loss / dataset_size

        save_model(model, str(checkpoint_path / 'params_{:04}.pt'.format(epoch + 1)))
        torch.save(optimizer.state_dict(), str(checkpoint_path / 'optim_{:04}.pt'.format(epoch + 1)))

        print('Epoch {:<4} Loss: {:.4f}'.format(epoch, epoch_loss))
        print()

        # Eval in each epoch
        pcks = eval_model(model, alphas, dataloader['test'])
        for i in range(len(alphas)):
            pck_dict = {cls: single_pck for cls, single_pck in zip(dataloader['train'].dataset.classes, pcks[:, i])}
            pck_dict['average'] = torch.mean(pcks[:, i])
            tfboard_writer.add_scalars(
                'Eval PCK@{:.2f}'.format(alphas[i]),
                pck_dict,
                (epoch + 1) * cfg.TRAIN.EPOCH_ITERS
            )

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}h {:.0f}m {:.0f}s'
          .format(time_elapsed // 3600, (time_elapsed // 60) % 60, time_elapsed % 60))

    return model


if __name__ == '__main__':
    from utils.dup_stdout_manager import DupStdoutFileManager
    from utils.parse_args import parse_args

    args = parse_args('Deep learning of graph matching training & evaluation code.')

    import importlib
    mod = importlib.import_module(cfg.MODULE)
    Net = mod.Net

    torch.manual_seed(cfg.RANDOM_SEED)

    dataset_len = {'train': cfg.TRAIN.EPOCH_ITERS, 'test': cfg.EVAL.EPOCH_ITERS}
    image_dataset = {
        x: GMDataset('PascalVOC',
                     sets=x,
                     length=dataset_len[x],
                     pad=cfg.PAIR.PADDING,
                     obj_resize=cfg.PAIR.RESCALE)
        for x in ('train', 'test')}
    dataloader = {x: get_dataloader(image_dataset[x], fix_seed=(x == 'test'))
        for x in ('train', 'test')}

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    model = Net()
    model = model.cuda()
    model = DataParallel(model, device_ids=cfg.GPUS)

    criterion = RobustLoss(norm=cfg.TRAIN.RLOSS_NORM)

    optimizer = optim.SGD(model.parameters(), lr=cfg.TRAIN.LR, momentum=cfg.TRAIN.MOMENTUM)

    if not Path(cfg.OUTPUT_PATH).exists():
        Path(cfg.OUTPUT_PATH).mkdir(parents=True)

    now_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    tfboardwriter = SummaryWriter(log_dir=str(Path(cfg.OUTPUT_PATH) / 'tensorboard' / 'training_{}'.format(now_time)))

    with DupStdoutFileManager(str(Path(cfg.OUTPUT_PATH) / ('train_log_' + now_time + '.log'))) as _:
        model = train_eval_model(model, criterion, optimizer, dataloader, tfboardwriter,
                                 num_epochs=cfg.TRAIN.NUM_EPOCHS,
                                 resume=cfg.TRAIN.START_EPOCH != 0,
                                 start_epoch=cfg.TRAIN.START_EPOCH)
