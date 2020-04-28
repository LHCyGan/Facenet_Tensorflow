"""Functions for building the face recognition network.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from subprocess import Popen, PIPE
import tensorflow as tf
from tensorflow.python.framework import ops
import numpy as np
from numpy import *
from scipy import misc
from sklearn.model_selection import KFold
from scipy import interpolate
from tensorflow.python.training import training
import random
import re
from tensorflow.python.platform import gfile
from six import iteritems


def triplet_loss(anchor, positive, negative, alpha):
    """Calculate the triplet loss according to the FaceNet paper
    
    Args:
      anchor: the embeddings for the anchor images.
      positive: the embeddings for the positive images.
      negative: the embeddings for the negative images.
  
    Returns:
      the triplet loss according to the FaceNet paper as a float tensor.
    """
    with tf.variable_scope('triplet_loss'):
        pos_dist = tf.reduce_sum(tf.square(tf.subtract(anchor, positive)), 1)
        neg_dist = tf.reduce_sum(tf.square(tf.subtract(anchor, negative)), 1)

        basic_loss = tf.add(tf.subtract(pos_dist, neg_dist), alpha)
        loss = tf.reduce_mean(tf.maximum(basic_loss, 0.0), 0)

    return loss


def decov_loss(xs):
    """Decov loss as described in https://arxiv.org/pdf/1511.06068.pdf
    'Reducing Overfitting In Deep Networks by Decorrelating Representation'
    """
    x = tf.reshape(xs, [int(xs.get_shape()[0]), -1])
    m = tf.reduce_mean(x, 0, True)
    z = tf.expand_dims(x - m, 2)
    corr = tf.reduce_mean(tf.matmul(z, tf.transpose(z, perm=[0, 2, 1])), 0)
    corr_frob_sqr = tf.reduce_sum(tf.square(corr))
    corr_diag_sqr = tf.reduce_sum(tf.square(tf.diag_part(corr)))
    loss = 0.5 * (corr_frob_sqr - corr_diag_sqr)
    return loss


def center_loss(features, label, alfa, nrof_classes):
    """Center loss based on the paper "A Discriminative Feature Learning Approach for Deep Face Recognition"
    """
    nrof_features = features.get_shape()[1]
    centers = tf.get_variable('centers', [nrof_classes, nrof_features], dtype=tf.float32,
                              initializer=tf.constant_initializer(0), trainable=False)
    label = tf.reshape(label, [-1])  # 将特征reshape成一维
    centers_batch = tf.gather(centers, label)  # 获取当前batch每个样本对应的中心
    diff = (1 - alfa) * (centers_batch - features)
    centers = tf.scatter_sub(centers, label, diff)
    loss = tf.reduce_mean(tf.square(features - centers_batch))
    return loss, centers


def get_image_paths_and_labels(dataset):
    image_paths_flat = []
    labels_flat = []
    for i in range(len(dataset)):
        image_paths_flat += dataset[i].image_paths
        labels_flat += [i] * len(dataset[i].image_paths)
    return image_paths_flat, labels_flat


def shuffle_examples(image_paths, labels):
    shuffle_list = list(zip(image_paths, labels))
    random.shuffle(shuffle_list)
    image_paths_shuff, labels_shuff = zip(*shuffle_list)
    return image_paths_shuff, labels_shuff


def read_images_from_disk(input_queue):
    """Consumes a single filename and label as a ' '-delimited string.
    Args:
      filename_and_label_tensor: A scalar string tensor.
    Returns:
      Two tensors: the decoded image, and the string label.
    """
    label = input_queue[1]
    file_contents = tf.read_file(input_queue[0])
    example = tf.image.decode_image(file_contents, channels=3)
    return example, label


def random_rotate_image(image):
    angle = np.random.uniform(low=-10.0, high=10.0)
    return misc.imrotate(image, angle, 'bicubic')


def read_and_augment_data(image_list, label_list, image_size, batch_size, max_nrof_epochs,
                          random_crop, random_flip, random_rotate, nrof_preprocess_threads, shuffle=True):
    images = ops.convert_to_tensor(image_list, dtype=tf.string)
    labels = ops.convert_to_tensor(label_list, dtype=tf.int32)

    # Makes an input queue
    input_queue = tf.train.slice_input_producer([images, labels],
                                                num_epochs=max_nrof_epochs, shuffle=shuffle)

    images_and_labels = []
    for _ in range(nrof_preprocess_threads):
        image, label = read_images_from_disk(input_queue)
        if random_rotate:
            image = tf.py_func(random_rotate_image, [image], tf.uint8)
        if random_crop:
            image = tf.random_crop(image, [image_size, image_size, 3])
        else:
            image = tf.image.resize_image_with_crop_or_pad(image, image_size, image_size)
        if random_flip:
            image = tf.image.random_flip_left_right(image)
        # pylint: disable=no-member
        image.set_shape((image_size, image_size, 3))
        image = tf.image.per_image_standardization(image)
        images_and_labels.append([image, label])

    image_batch, label_batch = tf.train.batch_join(
        images_and_labels, batch_size=batch_size,
        capacity=4 * nrof_preprocess_threads * batch_size,
        allow_smaller_final_batch=True)

    return image_batch, label_batch


def _add_loss_summaries(total_loss):
    """Add summaries for losses.
  
    Generates moving average for all losses and associated summaries for
    visualizing the performance of the network.
  
    Args:
      total_loss: Total loss from loss().
    Returns:
      loss_averages_op: op for generating moving averages of losses.
    """
    # Compute the moving average of all individual losses and the total loss.
    loss_averages = tf.train.ExponentialMovingAverage(0.9, name='avg')
    losses = tf.get_collection('losses')
    loss_averages_op = loss_averages.apply(losses + [total_loss])

    # Attach a scalar summmary to all individual losses and the total loss; do the
    # same for the averaged version of the losses.
    for l in losses + [total_loss]:
        # Name each loss as '(raw)' and name the moving average version of the loss
        # as the original loss name.
        tf.summary.scalar(l.op.name + ' (raw)', l)
        tf.summary.scalar(l.op.name, loss_averages.average(l))

    return loss_averages_op


def train(total_loss, global_step, optimizer, learning_rate, moving_average_decay, update_gradient_vars,
          log_histograms=True):
    # Generate moving averages of all losses and associated summaries.
    loss_averages_op = _add_loss_summaries(total_loss)

    # Compute gradients.
    with tf.control_dependencies([loss_averages_op]):
        if optimizer == 'ADAGRAD':
            opt = tf.train.AdagradOptimizer(learning_rate)
        elif optimizer == 'ADADELTA':
            opt = tf.train.AdadeltaOptimizer(learning_rate, rho=0.9, epsilon=1e-6)
        elif optimizer == 'ADAM':
            opt = tf.train.AdamOptimizer(learning_rate, beta1=0.9, beta2=0.999, epsilon=0.1)
        elif optimizer == 'RMSPROP':
            opt = tf.train.RMSPropOptimizer(learning_rate, decay=0.9, momentum=0.9, epsilon=1.0)
        elif optimizer == 'MOM':
            opt = tf.train.MomentumOptimizer(learning_rate, 0.9, use_nesterov=True)
        else:
            raise ValueError('Invalid optimization algorithm')

        grads = opt.compute_gradients(total_loss, update_gradient_vars)

    # Apply gradients.
    apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)

    # Add histograms for trainable variables.
    if log_histograms:
        for var in tf.trainable_variables():
            tf.summary.histogram(var.op.name, var)

    # Add histograms for gradients.
    if log_histograms:
        for grad, var in grads:
            if grad is not None:
                tf.summary.histogram(var.op.name + '/gradients', grad)

    # Track the moving averages of all trainable variables.
    variable_averages = tf.train.ExponentialMovingAverage(
        moving_average_decay, global_step)
    variables_averages_op = variable_averages.apply(tf.trainable_variables())

    with tf.control_dependencies([apply_gradient_op, variables_averages_op]):
        train_op = tf.no_op(name='train')

    return train_op


def prewhiten(x):
    mean = np.mean(x)
    std = np.std(x)
    std_adj = np.maximum(std, 1.0 / np.sqrt(x.size))
    y = np.multiply(np.subtract(x, mean), 1 / std_adj)
    return y


def crop(image, random_crop, image_size):
    if image.shape[1] > image_size:
        sz1 = int(image.shape[1] // 2)
        sz2 = int(image_size // 2)
        if random_crop:
            diff = sz1 - sz2
            (h, v) = (np.random.randint(-diff, diff + 1), np.random.randint(-diff, diff + 1))
        else:
            (h, v) = (0, 0)
        image = image[(sz1 - sz2 + v):(sz1 + sz2 + v), (sz1 - sz2 + h):(sz1 + sz2 + h), :]
    return image


def flip(image, random_flip):
    if random_flip and np.random.choice([True, False]):
        image = np.fliplr(image)
    return image


def to_rgb(img):
    w, h = img.shape
    ret = np.empty((w, h, 3), dtype=np.uint8)
    ret[:, :, 0] = ret[:, :, 1] = ret[:, :, 2] = img
    return ret


def load_data(image_paths, do_random_crop, do_random_flip, image_size, do_prewhiten=True):
    nrof_samples = len(image_paths)
    images = np.zeros((nrof_samples, image_size, image_size, 3))
    for i in range(nrof_samples):
        img = misc.imread(image_paths[i])
        if img.ndim == 2:
            img = to_rgb(img)
        if do_prewhiten:
            img = prewhiten(img)
        img = crop(img, do_random_crop, image_size)
        img = flip(img, do_random_flip)
        images[i, :, :, :] = img
    return images


def get_label_batch(label_data, batch_size, batch_index):
    nrof_examples = np.size(label_data, 0)
    j = batch_index * batch_size % nrof_examples
    if j + batch_size <= nrof_examples:
        batch = label_data[j:j + batch_size]
    else:
        x1 = label_data[j:nrof_examples]
        x2 = label_data[0:nrof_examples - j]
        batch = np.vstack([x1, x2])
    batch_int = batch.astype(np.int64)
    return batch_int


def get_batch(image_data, batch_size, batch_index):
    nrof_examples = np.size(image_data, 0)
    j = batch_index * batch_size % nrof_examples
    if j + batch_size <= nrof_examples:
        batch = image_data[j:j + batch_size, :, :, :]
    else:
        x1 = image_data[j:nrof_examples, :, :, :]
        x2 = image_data[0:nrof_examples - j, :, :, :]
        batch = np.vstack([x1, x2])
    batch_float = batch.astype(np.float32)
    return batch_float


def get_triplet_batch(triplets, batch_index, batch_size):
    ax, px, nx = triplets
    a = get_batch(ax, int(batch_size / 3), batch_index)
    p = get_batch(px, int(batch_size / 3), batch_index)
    n = get_batch(nx, int(batch_size / 3), batch_index)
    batch = np.vstack([a, p, n])
    return batch


def get_learning_rate_from_file(filename, epoch):
    with open(filename, 'r') as f:
        for line in f.readlines():
            line = line.split('#', 1)[0]
            if line:
                par = line.strip().split(':')
                e = int(par[0])
                lr = float(par[1])
                if e <= epoch:
                    learning_rate = lr
                else:
                    return learning_rate


class ImageClass():
    "Stores the paths to images for a given class"

    def __init__(self, name, image_paths):
        self.name = name
        self.image_paths = image_paths

    def __str__(self):
        return self.name + ', ' + str(len(self.image_paths)) + ' images'

    def __len__(self):
        return len(self.image_paths)


def get_dataset(path, has_class_directories=True):
    dataset = []
    path_exp = os.path.expanduser(path)
    classes = [path for path in os.listdir(path_exp)
               if os.path.isdir(os.path.join(path_exp, path))]
    classes.sort()
    nrof_classes = len(classes)
    for i in range(nrof_classes):
        class_name = classes[i]
        facedir = os.path.join(path_exp, class_name)
        image_paths = get_image_paths(facedir)
        dataset.append(ImageClass(class_name, image_paths))

    return dataset


def get_image_paths(facedir):
    image_paths = []
    if os.path.isdir(facedir):
        images = os.listdir(facedir)
        image_paths = [os.path.join(facedir, img) for img in images]
    return image_paths


def split_dataset(dataset, split_ratio, mode):
    if mode == 'SPLIT_CLASSES':
        nrof_classes = len(dataset)
        class_indices = np.arange(nrof_classes)
        np.random.shuffle(class_indices)
        split = int(round(nrof_classes * split_ratio))
        train_set = [dataset[i] for i in class_indices[0:split]]
        test_set = [dataset[i] for i in class_indices[split:-1]]
    elif mode == 'SPLIT_IMAGES':
        train_set = []
        test_set = []
        min_nrof_images = 2
        for cls in dataset:
            paths = cls.image_paths
            np.random.shuffle(paths)
            split = int(round(len(paths) * split_ratio))
            if split < min_nrof_images:
                continue  # Not enough images for test set. Skip class...
            train_set.append(ImageClass(cls.name, paths[0:split]))
            test_set.append(ImageClass(cls.name, paths[split:-1]))
    else:
        raise ValueError('Invalid train/test split mode "%s"' % mode)
    return train_set, test_set


def load_model(model):
    # Check if the model is a model directory (containing a metagraph and a checkpoint file)
    #  or if it is a protobuf file with a frozen graph
    model_exp = os.path.expanduser(model)  # 把path中包含的"~"和"~user"转换成用户目录
    if (os.path.isfile(model_exp)):
        print('Model filename: %s' % model_exp)
        with gfile.FastGFile(model_exp, 'rb') as f:
            graph_def = tf.GraphDef()  # 文件读和写
            graph_def.ParseFromString(f.read())
            tf.import_graph_def(graph_def, name='')
    else:
        print('Model directory: %s' % model_exp)
        meta_file, ckpt_file = get_model_filenames(model_exp)

        print('Metagraph file: %s' % meta_file)
        print('Checkpoint file: %s' % ckpt_file)

        saver = tf.train.import_meta_graph(os.path.join(model_exp, meta_file))
        saver.restore(tf.get_default_session(), os.path.join(model_exp, ckpt_file))


def get_model_filenames(model_dir):
    files = os.listdir(model_dir)
    meta_files = [s for s in files if s.endswith('.meta')]
    if len(meta_files) == 0:
        raise ValueError('No meta file found in the model directory (%s)' % model_dir)
    elif len(meta_files) > 1:
        raise ValueError('There should not be more than one meta file in the model directory (%s)' % model_dir)
    meta_file = meta_files[0]
    ckpt = tf.train.get_checkpoint_state(model_dir)
    if ckpt and ckpt.model_checkpoint_path:
        ckpt_file = os.path.basename(ckpt.model_checkpoint_path)
        return meta_file, ckpt_file

    meta_files = [s for s in files if '.ckpt' in s]
    max_step = -1
    for f in files:
        step_str = re.match(r'(^model-[\w\- ]+.ckpt-(\d+))', f)
        if step_str is not None and len(step_str.groups()) >= 2:
            step = int(step_str.groups()[1])
            if step > max_step:
                max_step = step
                ckpt_file = step_str.groups()[0]
    return meta_file, ckpt_file


def calculate_roc(thresholds, embeddings1, embeddings2, actual_issame, nrof_folds=10):
    assert (embeddings1.shape[0] == embeddings2.shape[0])  # assert的异常参数,如果相等就抛出异常
    assert (embeddings1.shape[1] == embeddings2.shape[1])
    nrof_pairs = min(len(actual_issame), embeddings1.shape[0])  # embeddings1.shape[0] = 6000
    nrof_thresholds = len(thresholds)  # nrof_thresholds = 400
    print(embeddings1.shape[0])
    print('wokao')
    print(nrof_thresholds)
    k_fold = KFold(n_splits=nrof_folds, shuffle=False)  # nrof_folds= 10.训练和测试分成10文件夹，一个文件夹训练，9个测试，循环多次

    tprs = np.zeros((nrof_folds, nrof_thresholds))
    fprs = np.zeros((nrof_folds, nrof_thresholds))
    accuracy = np.zeros((nrof_folds))  # nrof_folds= 10,10个精度的值
    # dist = []  # 声明固定大小的空矩阵12000*128
    # #官方自带的计算欧式距离，选择最佳的距离
    # diff = np.subtract(embeddings1, embeddings2)#从第一个数组中减去第二个数组中的元素
    # dist = np.sum(np.square(diff),1)#sum应该是默认的axis=0 ,axis=1以后就是将一个矩阵的每一行向量相加
    # print('dist is :')
    # print(dist)
    # print(len(dist)) #dist的容量为6000
    # 官方自带的计算欧式距离,修改加根号
    # diff = np.subtract(embeddings1, embeddings2)#从第一个数组中减去第二个数组中的元素
    # dist = np.sqrt(np.sum(np.square(diff),1))#为啥加1呢？
    # print('dist is :')
    # print(dist)
    # print(np.shape(dist))
    # print('max is :')
    # print(max(dist))
    # print(min(dist))
    # print(len(dist)) #dist的容量为6000
    # 计算余弦距离，
    dist = []  # 声明固定大小的空矩阵12000*128
    for i in range(len(actual_issame)):
        num = dot(embeddings1[i], embeddings2[i])
        denom = linalg.norm(embeddings1[i]) * linalg.norm(embeddings2[i])  # norm则表示范数
        cos = num / denom  # 余弦值
        a = 0.5 + 0.5 * cos  # 归一化，，余弦距离
        # print(a) #dist的容量为6000
        dist.append(a)
    dist = np.array(dist)
    print('the nums of dist is:')
    print(len(dist))
    print('dist is:')
    # print(dist)
    print(max(dist))
    print(min(dist))
    indices = np.arange(nrof_pairs)

    for fold_idx, (train_set, test_set) in enumerate(k_fold.split(indices)):

        # Find the best threshold for the fold
        acc_train = np.zeros((nrof_thresholds))
        for threshold_idx, threshold in enumerate(thresholds):  # enumerate对一个列表，既要遍历索引又要遍历元素时
            _, _, acc_train[threshold_idx] = calculate_accuracy(threshold, dist[train_set], actual_issame[train_set])
        best_threshold_index = np.argmax(acc_train)  # argmax返回的是最大数的索引
        print('best_threshold index is')
        print(best_threshold_index)
        for threshold_idx, threshold in enumerate(thresholds):
            tprs[fold_idx, threshold_idx], fprs[fold_idx, threshold_idx], _ = calculate_accuracy(threshold,
                                                                                                 dist[test_set],
                                                                                                 actual_issame[
                                                                                                     test_set])
        _, _, accuracy[fold_idx] = calculate_accuracy(thresholds[best_threshold_index], dist[test_set],
                                                      actual_issame[test_set])

    print('best_threshold is :')
    print(thresholds[best_threshold_index])
    best_threshold = thresholds[best_threshold_index]
    tpr = np.mean(tprs, 0)
    fpr = np.mean(fprs, 0)
    return tpr, fpr, accuracy, best_threshold


def calculate_accuracy(threshold, dist, actual_issame):
    # predict_issame = np.less(dist, threshold)#x<y返回True，否则返回False。，欧式距离
    # print('the nun train：')
    # print(len(dist))#一次600
    predict_issame = np.less(threshold, dist)  # x<y返回True，否则返回False。，余弦距离
    tp = np.sum(np.logical_and(predict_issame, actual_issame))  # logical_and逻辑操作np.logical_and(True, False) 结果是False
    fp = np.sum(np.logical_and(predict_issame, np.logical_not(actual_issame)))
    tn = np.sum(np.logical_and(np.logical_not(predict_issame), np.logical_not(actual_issame)))
    fn = np.sum(np.logical_and(np.logical_not(predict_issame),
                               actual_issame))  # np.logical_not(逻辑非) np.logical_not([True, False, 0, 1])
    # 逻辑与操作是指只有两个都是真的时候才是真，其余均为假。                                                                                                 #array([False,  True,  True, False], dtype=bool)
    # tp为预测为真的与真实值为真的结果总和个数
    # tn为预测为假的与真实值为假的结果总和个数，两者相加即为预测正确的个数。
    # print('calculate is:')
    # print(tp)#299#0
    # print(tn)#298#300
    tpr = 0 if (tp + fn == 0) else float(tp) / float(tp + fn)
    fpr = 0 if (fp + tn == 0) else float(fp) / float(fp + tn)
    acc = float(tp + tn) / dist.size
    return tpr, fpr, acc


def calculate_val(thresholds, threshold_acc, embeddings1, embeddings2, actual_issame, far_target, nrof_folds=10):
    assert (embeddings1.shape[0] == embeddings2.shape[0])
    assert (embeddings1.shape[1] == embeddings2.shape[1])
    nrof_pairs = min(len(actual_issame), embeddings1.shape[0])
    nrof_thresholds = len(thresholds)
    k_fold = KFold(n_splits=nrof_folds, shuffle=False)

    val = np.zeros(nrof_folds)
    far = np.zeros(nrof_folds)

    # 计算余弦距离，
    # dist = []  # 声明固定大小的空矩阵12000*128
    # for i in range(len(actual_issame)):
    #     num = dot(embeddings1[i], embeddings2[i])
    #     denom = linalg.norm(embeddings1[i]) * linalg.norm(embeddings2[i])  # norm则表示范数
    #     cos = num / denom  # 余弦值
    #     a = 0.5 + 0.5 * cos  # 归一化，，余弦距离
    #     # print(a)  # dist的容量为6000
    #     # dist[i] = a
    #     dist.append(a)
    # dist = np.array(dist)
    # print('dist is:')
    # print(max(dist))
    # print(min(dist))
    # 计算欧式距离
    diff = np.subtract(embeddings1, embeddings2)
    dist = np.sum(np.square(diff), 1)

    indices = np.arange(nrof_pairs)

    for fold_idx, (train_set, test_set) in enumerate(k_fold.split(indices)):

        # Find the threshold that gives FAR = far_target
        far_train = np.zeros(nrof_thresholds)
        for threshold_idx, threshold in enumerate(thresholds):
            _, far_train[threshold_idx] = calculate_val_far(threshold, dist[train_set], actual_issame[train_set])
        if np.max(far_train) >= far_target:
            f = interpolate.interp1d(far_train, thresholds, kind='slinear')
            threshold = f(far_target)
        else:
            threshold = 0.0
        # threshold = threshold_acc
        print('best threshold is')
        print(threshold)
        val[fold_idx], far[fold_idx] = calculate_val_far(threshold, dist[test_set], actual_issame[test_set])

    val_mean = np.mean(val)
    far_mean = np.mean(far)
    val_std = np.std(val)
    return val_mean, val_std, far_mean


def calculate_val_far(threshold, dist, actual_issame):
    predict_issame = np.less(dist, threshold)
    true_accept = np.sum(np.logical_and(predict_issame, actual_issame))  # 预测的与实际的一致
    false_accept = np.sum(np.logical_and(predict_issame, np.logical_not(actual_issame)))
    n_same = np.sum(actual_issame)
    n_diff = np.sum(np.logical_not(actual_issame))
    val = float(true_accept) / float(n_same)  # 是一对的预测为一对的，
    far = float(false_accept) / float(n_diff)  # 真实值为假预测为真
    return val, far


def store_revision_info(src_path, output_dir, arg_string):
    try:
        # Get git hash
        cmd = ['git', 'rev-parse', 'HEAD']
        gitproc = Popen(cmd, stdout=PIPE, cwd=src_path)
        (stdout, _) = gitproc.communicate()
        git_hash = stdout.strip()
    except OSError as e:
        git_hash = ' '.join(cmd) + ': ' + e.strerror

    try:
        # Get local changes
        cmd = ['git', 'diff', 'HEAD']
        gitproc = Popen(cmd, stdout=PIPE, cwd=src_path)
        (stdout, _) = gitproc.communicate()
        git_diff = stdout.strip()
    except OSError as e:
        git_diff = ' '.join(cmd) + ': ' + e.strerror

    # Store a text file in the log directory
    rev_info_filename = os.path.join(output_dir, 'revision_info.txt')
    with open(rev_info_filename, "w") as text_file:
        text_file.write('arguments: %s\n--------------------\n' % arg_string)
        text_file.write('tensorflow version: %s\n--------------------\n' % tf.__version__)  # @UndefinedVariable
        text_file.write('git hash: %s\n--------------------\n' % git_hash)
        text_file.write('%s' % git_diff)


def list_variables(filename):
    reader = training.NewCheckpointReader(filename)
    variable_map = reader.get_variable_to_shape_map()
    names = sorted(variable_map.keys())
    return names


def put_images_on_grid(images, shape=(16, 8)):
    nrof_images = images.shape[0]
    img_size = images.shape[1]
    bw = 3
    img = np.zeros((shape[1] * (img_size + bw) + bw, shape[0] * (img_size + bw) + bw, 3), np.float32)
    for i in range(shape[1]):
        x_start = i * (img_size + bw) + bw
        for j in range(shape[0]):
            img_index = i * shape[0] + j
            if img_index >= nrof_images:
                break
            y_start = j * (img_size + bw) + bw
            img[x_start:x_start + img_size, y_start:y_start + img_size, :] = images[img_index, :, :, :]
        if img_index >= nrof_images:
            break
    return img


def write_arguments_to_file(args, filename):
    with open(filename, 'w') as f:
        for key, value in iteritems(vars(args)):
            f.write('%s: %s\n' % (key, str(value)))
