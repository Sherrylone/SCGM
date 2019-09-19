import os
from PIL import Image
import numpy as np
import random
import pickle

from data.base_dataset import BaseDataset
from utils.config import cfg


class CUB2011(BaseDataset):
    """Data loader for CUB-2011 dataset. Follows train/test split and pair
    annotations as given by the dataset. Please make sure to configure a correct
    `cfg.CUB2011.ROOT_PATH` to make use of this class. Additionally, please note
    that UCN is evaluated on cleaned up test pairs as provided by WarpNet.
    Please refer to `CUB2011DB_WarpNetTest` class for details.

    This function is modified from UCN implementation of CUB2011.
    """
    set_data = {'train': [], 'test': []}
    classes = []

    _set_pairs = {}
    _set_mask = {}

    def __init__(self, sets, obj_resize):
        rootpath = cfg.CUB2011.ROOT_PATH
        with open(os.path.join(rootpath, 'images.txt')) as f:
            self.im2fn = dict(l.rstrip('\n').split() for l in f.readlines())
        with open(os.path.join(rootpath, 'train_test_split.txt')) as f:
            train_split = dict(l.rstrip('\n').split() for l in f.readlines())
        with open(os.path.join(rootpath, 'classes.txt')) as f:
            classes = dict(l.rstrip('\n').split() for l in f.readlines())
        with open(os.path.join(rootpath, 'image_class_labels.txt')) as f:
            img2class = [l.rstrip('\n').split() for l in f.readlines()]
            img_idxs, class_idxs = map(list, zip(*img2class))
            class2img = lists2dict(class_idxs, img_idxs)
        with open(os.path.join(rootpath, 'parts', 'part_locs.txt')) as f:
            part_locs = [l.rstrip('\n').split() for l in f.readlines()]
            fi, pi, x, y, v = map(list, zip(*part_locs))
            self.im2kpts = lists2dict(fi, zip(pi, x, y, v))
        with open(os.path.join(rootpath, 'bounding_boxes.txt')) as f:
            bboxes = [l.rstrip('\n').split() for l in f.readlines()]
            ii, x, y, w, h = map(list, zip(*bboxes))
            self.im2bbox = dict(zip(ii, zip(x, y, w, h)))
        if not cfg.CUB2011.CROSS_CATEGORY_MATCHING:
            for class_idx in sorted(classes):
                self.classes.append(classes[class_idx])
                train_set = []
                test_set = []
                for img_idx in class2img[class_idx]:
                    if train_split[img_idx] == '1':
                        train_set.append(img_idx)
                    else:
                        test_set.append(img_idx)
                self.set_data['train'].append(train_set)
                self.set_data['test'].append(test_set)
        else:
            self.classes.append('cub2011')
            self.set_data['train'].append([])
            self.set_data['test'].append([])
            for class_idx in sorted(classes):
                train_set = []
                test_set = []
                for img_idx in class2img[class_idx]:
                    if train_split[img_idx] == '1':
                        train_set.append(img_idx)
                    else:
                        test_set.append(img_idx)
                self.set_data['train'][0] += train_set
                self.set_data['test'][0] += test_set
        self.sets = sets
        self.obj_resize = obj_resize

        super(CUB2011, self).__init__()

    def get_imgname(self, data):
        return os.path.join(cfg.CUB2011.ROOT_PATH, 'images', self.im2fn[data])

    def get_meta(self, data):
        pi, x, y, v = map(list, zip(*self.im2kpts[data]))
        order = np.argsort(np.array(pi).astype(int))
        keypts = np.array([np.array(x).astype('float')[order],
                           np.array(y).astype('float')[order]])
        visible = np.array(v).astype('uint8')[order]
        bbox = np.array(self.im2bbox[data]).astype(float)
        return keypts, visible, bbox

    def get_pair(self, cls=None, shuffle=True):
        """
        Randomly get a pair of objects from CUB-2011
        :param cls: None for random class, or specify for a certain set
        :param shuffle: random shuffle the keypoints
        :return: (pair of data, groundtruth permutation matrix)
        """
        assert self.sets in self.set_data.keys()

        if cls is None:
            cls = random.randrange(0, len(self.classes))
        elif type(cls) == str:
            cls = self.classes.index(cls)
        assert type(cls) == int and 0 <= cls < len(self.classes)

        anno_pair = []
        for img_name in random.sample(self.set_data[self.sets][cls], 2):
            anno_dict = self.__get_anno_dict(img_name, cls)
            if shuffle:
                random.shuffle(anno_dict['keypoints'])
            anno_pair.append(anno_dict)

        perm_mat = np.zeros([len(_['keypoints']) for _ in anno_pair], dtype=np.float32)
        row_list = []
        col_list = []
        for i, keypoint in enumerate(anno_pair[0]['keypoints']):
            for j, _keypoint in enumerate(anno_pair[1]['keypoints']):
                if keypoint['name'] == _keypoint['name']:
                    perm_mat[i, j] = 1
                    row_list.append(i)
                    col_list.append(j)
                    break
        row_list.sort()
        col_list.sort()
        perm_mat = perm_mat[row_list, :]
        perm_mat = perm_mat[:, col_list]
        anno_pair[0]['keypoints'] = [anno_pair[0]['keypoints'][i] for i in row_list]
        anno_pair[1]['keypoints'] = [anno_pair[1]['keypoints'][j] for j in col_list]

        return anno_pair, perm_mat

    def __get_anno_dict(self, img_name, cls):
        keypts, visible, bbox = self.get_meta(img_name)

        xmin, ymin, w, h = bbox

        img_file = self.get_imgname(img_name)
        with Image.open(str(img_file)) as img:
            ori_sizes = img.size
            try:
                obj = img.resize(self.obj_resize, resample=Image.BICUBIC, box=(xmin, ymin, xmin + w, ymin + h))
            except ValueError:
                xmin, xmax = np.clip((xmin, xmin + w), 0, img.size[0])
                ymin, ymax = np.clip((ymin, ymin + w), 0, img.size[1])
                obj = img.resize(self.obj_resize, resample=Image.BICUBIC, box=(xmin, ymin, xmax, ymax))

        if not obj.mode == 'RGB':
            obj = obj.convert('RGB')

        keypoint_list = []
        for keypt_idx in range(keypts.shape[1]):
            if visible[keypt_idx]:
                attr = dict()
                attr['x'] = (keypts[0, keypt_idx] - xmin) * self.obj_resize[0] / w
                attr['y'] = (keypts[1, keypt_idx] - ymin) * self.obj_resize[1] / h
                attr['name'] = keypt_idx
                keypoint_list.append(attr)

        anno_dict = dict()
        anno_dict['image'] = obj
        anno_dict['keypoints'] = keypoint_list
        anno_dict['bounds'] = xmin, ymin, w, h
        anno_dict['ori_sizes'] = ori_sizes
        anno_dict['cls'] = self.classes[cls]

        return anno_dict


def lists2dict(keys, vals):
    ans = {}
    for idx, val_i in enumerate(vals):
        if keys[idx] in ans:
            ans[keys[idx]].append(val_i)
        else:
            ans[keys[idx]] = [val_i]
    return ans
