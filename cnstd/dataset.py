# coding: utf-8
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import os
from pathlib import Path
from typing import Optional, Union, List, Tuple, Callable

import numpy as np
import pytorch_lightning as pt
import torch
from torch.utils.data import DataLoader, Dataset

from .utils import read_charset, imread, normalize_img_array, imsave
from .transforms.process_data import PROCESSORS


def read_idx_file(idx_fp):
    img_label_pairs = []
    with open(idx_fp) as f:
        for line in f:
            img_fp, gt_fp = line.strip().split('\t')
            img_label_pairs.append((img_fp, gt_fp))
    return img_label_pairs


class StdDataset(Dataset):
    def __init__(self, index_fp, transforms, data_root_dir=None, mode='train'):
        super().__init__()
        img_gt_paths = read_idx_file(index_fp)
        self.img_paths, gt_paths = zip(*[
            (os.path.join(data_root_dir, img_fp), os.path.join(data_root_dir, gt_fp))
            for img_fp, gt_fp in img_gt_paths
        ])
        self.transforms = transforms

        self.length = len(self.img_paths)
        self.mode = mode
        self.targets = self.load_ann(gt_paths)
        if self.mode != 'test':
            assert len(self.img_paths) == len(self.targets)

    def load_ann(self, gt_paths):
        res = []
        for gt in gt_paths:
            lines = []
            reader = open(gt, 'r').readlines()
            for line in reader:
                item = {}
                parts = line.strip().split(',')
                label = parts[-1]
                line = [i.strip('\ufeff').strip('\xef\xbb\xbf') for i in parts]
                poly = np.array(list(map(float, line[:8])), dtype=np.float32).reshape((-1, 2))  # [4, 2]
                item['poly'] = poly
                item['text'] = label
                lines.append(item)
            res.append(lines)
        return res

    def __len__(self):
        return self.length

    def __getitem__(self, item):
        img_fp = self.img_paths[item]
        img = imread(img_fp)
        c, h, w = img.shape

        new_img = self.transforms(torch.from_numpy(img))  # return: [C, H, W]
        data = {'image': new_img.permute(1, 2, 0).numpy(), 'shape': (h, w)}
        new_h, new_w = data['image'].shape[:2]

        if self.mode != 'test':
            lines = self.targets[item]
            for item in lines:  # 转化到 0~1 之间的取值，去掉对resize的依赖
                item['poly'][:, 0] *= new_w / w
                item['poly'][:, 1] *= new_h / h
            data['lines'] = lines

            line_polys = []
            for line in data['lines']:
                new_poly = [(p[0], p[1]) for p in line['poly'].tolist()]
                line_polys.append({
                    'points': new_poly,
                    'ignore': line['text'] == '###',
                    'text': line['text'],
                })

            data['polys'] = line_polys
            data['is_training'] = True

            for processor in PROCESSORS:
                data = processor(data)

            data['image'] = normalize_img_array(data['image'])
        return data


def collate_fn(img_labels: List[Tuple[str, str]], transformers: Callable = None):
    test_mode = len(img_labels[0]) == 1
    if test_mode:
        img_list = zip(*img_labels)
        labels_list, label_lengths = None, None
    else:
        img_list, labels_list = zip(*img_labels)
        label_lengths = torch.tensor([len(labels) for labels in labels_list])

    img_lengths = torch.tensor([img.size(2) for img in img_list])
    if transformers is not None:
        img_list = [transformers(img) for img in img_list]
    imgs = pad_img_seq(img_list)
    return imgs, img_lengths, labels_list, label_lengths


class StdDataModule(pt.LightningDataModule):
    def __init__(
        self,
        index_dir: Union[str, Path],
        vocab_fp: Union[str, Path],
        data_root_dir: Union[str, Path, None] = None,
        train_transforms=None,
        val_transforms=None,
        batch_size: int = 64,
        num_workers: int = 0,
        pin_memory: bool = False,
    ):
        super().__init__(
            train_transforms=train_transforms, val_transforms=val_transforms
        )
        self.vocab, self.letter2id = read_charset(vocab_fp)
        self.index_dir = Path(index_dir)
        self.data_root_dir = data_root_dir
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.pin_memory = pin_memory

        self.train = StdDataset(
            self.index_dir / 'train.tsv', self.train_transforms, self.data_root_dir, mode='train'
        )
        self.val = StdDataset(self.index_dir / 'dev.tsv', self.val_transforms, self.data_root_dir, mode='train')

    @property
    def vocab_size(self):
        return len(self.vocab)

    def prepare_data(self):
        # called only on 1 GPU
        pass

    def setup(self, stage: Optional[str] = None):
        # called on every GPU
        pass

    def train_dataloader(self):
        return DataLoader(
            self.train,
            batch_size=self.batch_size,
            shuffle=True,
            collate_fn=lambda x: collate_fn(x, self.train_transforms),
            num_workers=self.num_workers,
            pin_memory=self.pin_memory,
        )

    def val_dataloader(self):
        return DataLoader(
            self.val,
            batch_size=self.batch_size,
            shuffle=False,
            collate_fn=lambda x: collate_fn(x, self.val_transforms),
            num_workers=self.num_workers,
            pin_memory=self.pin_memory,
        )

    def test_dataloader(self):
        return None
