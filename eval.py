import time
from datetime import datetime
from pathlib import Path

from src.lap_solvers.hungarian import hungarian
from src.dataset.data_loader import GMDataset, get_dataloader
from src.evaluation_metric import *
from src.parallel import DataParallel
from src.utils.model_sl import load_model

from src.utils.config import cfg


def eval_model(model, alphas, dataloader, eval_epoch=None, verbose=False):
    print('Start evaluation...')
    since = time.time()

    device = next(model.parameters()).device

    if eval_epoch is not None:
        model_path = str(Path(cfg.OUTPUT_PATH) / 'params' / 'params_{:04}.pt'.format(eval_epoch))
        print('Loading model parameters from {}'.format(model_path))
        load_model(model, model_path)

    was_training = model.training
    model.eval()

    ds = dataloader.dataset
    classes = ds.classes
    cls_cache = ds.cls

    #lap_solver = BiStochastic(max_iter=20)
    lap_solver = hungarian

    pcks = torch.zeros(len(classes), len(alphas), device=device)
    accs = []
    precisions = []
    f1s = []
    objs = torch.zeros(len(classes), device=device)

    for i, cls in enumerate(classes):
        if verbose:
            print('Evaluating class {}: {}/{}'.format(cls, i, len(classes)))

        running_since = time.time()
        iter_num = 0

        ds.cls = cls
        pck_match_num = torch.zeros(len(alphas), device=device)
        pck_total_num = torch.zeros(len(alphas), device=device)
        acc_list = []
        precision_list = [] 
        f1_list = []
        obj_total_num = torch.zeros(1, device=device)
        for inputs in dataloader:
            if 'images' in inputs:
                data1, data2 = [_.cuda() for _ in inputs['images']]
                inp_type = 'img'
            elif 'features' in inputs:
                data1, data2 = [_.cuda() for _ in inputs['features']]
                inp_type = 'feat'
            else:
                raise ValueError('no valid data key (\'images\' or \'features\') found from dataloader!')
            P1_gt, P2_gt = [_.cuda() for _ in inputs['Ps']]
            n1_gt, n2_gt = [_.cuda() for _ in inputs['ns']]
            e1_gt, e2_gt = [_.cuda() for _ in inputs['es']]
            G1_gt, G2_gt = [_.cuda() for _ in inputs['Gs']]
            H1_gt, H2_gt = [_.cuda() for _ in inputs['Hs']]
            KG, KH = [_.cuda() for _ in inputs['Ks']]
            perm_mat = inputs['gt_perm_mat'].cuda()

            batch_num = data1.size(0)

            iter_num = iter_num + 1

            thres = torch.empty(batch_num, len(alphas), device=device)
            for b in range(batch_num):
                thres[b] = alphas * cfg.EVAL.PCK_L

            with torch.set_grad_enabled(False):
                pred = \
                    model(data1, data2, P1_gt, P2_gt, G1_gt, G2_gt, H1_gt, H2_gt, n1_gt, n2_gt, KG, KH, inp_type)
                if len(pred) == 2:
                    s_pred_score, d_pred = pred
                    affmtx = None
                else:
                    s_pred_score, d_pred, affmtx = pred

            if type(s_pred_score) is list:
                s_pred_score = s_pred_score[-1]
            s_pred_perm = lap_solver(s_pred_score, n1_gt, n2_gt)

            #_, _pck_match_num, _pck_total_num = pck(P2_gt, P2_gt, torch.bmm(s_pred_perm, perm_mat.transpose(1, 2)), thres, n1_gt)
            #pck_match_num += _pck_match_num
            #pck_total_num += _pck_total_num

            acc, _, __ = matching_accuracy(s_pred_perm, perm_mat, n1_gt)
            acc_list.append(acc)
            precision, _, __ = matching_precision(s_pred_perm, perm_mat, n1_gt)
            precision_list.append(precision)
            precision_list.append(precision)
            f1 = 2 * (precision * acc) / (precision + acc)
            f1[torch.isnan(f1)] = 0
            f1_list.append(f1)

            if affmtx is not None:
                obj_score = objective_score(s_pred_perm, affmtx, n1_gt)
                objs[i] += torch.sum(obj_score)
            obj_total_num += batch_num

            if iter_num % cfg.STATISTIC_STEP == 0 and verbose:
                running_speed = cfg.STATISTIC_STEP * batch_num / (time.time() - running_since)
                print('Class {:<8} Iteration {:<4} {:>4.2f}sample/s'.format(cls, iter_num, running_speed))
                running_since = time.time()

        pcks[i] = pck_match_num / pck_total_num
        accs.append(torch.cat(acc_list))
        precisions.append(torch.cat(precision_list))
        f1s.append(torch.cat(f1_list))
        objs[i] = objs[i] / obj_total_num
        if verbose:
            print('Class {} PCK@{{'.format(cls) +
                  ', '.join(list(map('{:.2f}'.format, alphas.tolist()))) + '} = {' +
                  ', '.join(list(map('{:.4f}'.format, pcks[i].tolist()))) + '}')
            print('Class {} {}'.format(cls, format_accuracy_metric(precisions[i], accs[i], f1s[i])))
            print('Class {} obj score = {:.4f}'.format(cls, objs[i]))

    time_elapsed = time.time() - since
    print('Evaluation complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))

    model.train(mode=was_training)
    ds.cls = cls_cache

    # print result
    for i in range(len(alphas)):
        print('PCK@{:.2f}'.format(alphas[i]))
        for cls, single_pck in zip(classes, pcks[:, i]):
            print('{} = {:.4f}'.format(cls, single_pck))
        print('average PCK = {:.4f}'.format(torch.mean(pcks[:, i])))

    print('Matching accuracy')
    for cls, cls_p, cls_acc, cls_f1 in zip(classes, precisions, accs, f1s):
        print('{}: {}'.format(cls, format_accuracy_metric(cls_p, cls_acc, cls_f1)))
    print('average accuracy: {}'.format(format_accuracy_metric(torch.cat(precisions), torch.cat(accs), torch.cat(f1s))))

    if not torch.any(torch.isnan(objs)):
        print('Objective score')
        for cls, cls_obj in zip(classes, objs):
            print('{} = {:.4f}'.format(cls, cls_obj))
        print('average objscore = {:.4f}'.format(torch.mean(objs)))

    return torch.Tensor(list(map(torch.mean, accs)))


if __name__ == '__main__':
    from src.utils.dup_stdout_manager import DupStdoutFileManager
    from src.utils.parse_args import parse_args
    from src.utils.print_easydict import print_easydict

    args = parse_args('Deep learning of graph matching evaluation code.')

    import importlib
    mod = importlib.import_module(cfg.MODULE)
    Net = mod.Net

    torch.manual_seed(cfg.RANDOM_SEED)

    image_dataset = GMDataset(cfg.DATASET_FULL_NAME,
                              sets='test',
                              length=cfg.EVAL.SAMPLES,
                              pad=cfg.PAIR.PADDING,
                              obj_resize=cfg.PAIR.RESCALE)
    dataloader = get_dataloader(image_dataset)

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    model = Net()
    model = model.to(device)
    model = DataParallel(model, device_ids=cfg.GPUS)

    if not Path(cfg.OUTPUT_PATH).exists():
        Path(cfg.OUTPUT_PATH).mkdir(parents=True)
    now_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    with DupStdoutFileManager(str(Path(cfg.OUTPUT_PATH) / ('eval_log_' + now_time + '.log'))) as _:
        print_easydict(cfg)
        alphas = torch.tensor(cfg.EVAL.PCK_ALPHAS, dtype=torch.float32, device=device)
        classes = dataloader.dataset.classes
        pcks = eval_model(model, alphas, dataloader,
                          eval_epoch=cfg.EVAL.EPOCH if cfg.EVAL.EPOCH != 0 else None,
                          verbose=True)
