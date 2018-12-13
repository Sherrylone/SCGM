import torch


def pck(x, x_gt, perm_mat, dist_threshs, ns):
    """
    Percentage of Correct Keypoints evaluation metric.
    :param x: candidate coordinates
    :param x_gt: ground truth coordinates
    :param perm_mat: permutation matrix or doubly stochastic matrix indicating correspondence
    :param dist_threshs: a iterable list of thresholds in pixel
    :param ns: number of exact pairs.
    :return: pck, matched num of pairs, total num of pairs
    """
    device = x.device
    batch_num = x.shape[0]
    thresh_num = dist_threshs.shape[1]

    indices = torch.argmax(perm_mat, dim=-1)

    dist = torch.zeros(batch_num, x_gt.shape[1], device=device)
    for b in range(batch_num):
        x_correspond = x[b, indices[b], :]
        dist[b, 0:ns[b]] = torch.norm(x_correspond - x_gt[b], p=2, dim=-1)[0:ns[b]]

    match_num = torch.zeros(thresh_num, device=device)
    total_num = torch.zeros(thresh_num, device=device)
    for b in range(batch_num):
        for idx in range(thresh_num):
            matches = (dist[b] < dist_threshs[b, idx])[0:ns[b]]
            match_num[idx] += torch.sum(matches).to(match_num.dtype)
            total_num[idx] += ns[b].to(total_num.dtype)

    return match_num / total_num, match_num, total_num
