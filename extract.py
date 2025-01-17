#!/usr/bin/env python
"""

"""

import os
import argparse
import json
import h5py
import time
import numpy as np
import torch
from torch.autograd import Variable
import torchvision
import torchvision.transforms as transforms

from utils import get_freer_gpu
from preprocess import resize_frame, preprocess_frame, ToTensorWithoutScaling, ToFloatTensorInZeroOne
from sample_frames import sample_frames, sample_frames2
from feature_extractors.cnn import CNN
from feature_extractors.c3d import C3D
from feature_extractors.i3dpt import I3D
from feature_extractors.eco import init_model as ECO
from feature_extractors.tsm import init_model as TSM
from configuration_file import ConfigurationFile

__author__ = "jssprz"
__version__ = "0.0.1"
__maintainer__ = "jssprz"
__email__ = "jperezmartin90@gmail.com"
__status__ = "Development"


def l2norm(X):
    """L2-normalize columns of X
    """
    norm = torch.pow(X, 2).sum(dim=1, keepdim=True).sqrt()
    X = torch.div(X, norm)
    return X

def parse_shift_option_from_log_name(log_name):
    if 'shift' in log_name:
        strings = log_name.split('_')
        for i, s in enumerate(strings):
            if 'shift' in s:
                break
        return True, int(strings[i].replace('shift', '')), strings[i + 1]
    else:
        return False, None, None

def extract_features(file_name, extractor_name, extractor, dataset_name, device, config, feature_size, transformer):
    """

    :type c3d_extractor:
    :param device:
    :param config:
    :param frame_shape:
    :param dataset_name:
    :param cnn_extractor:
    :param c3d_extractor:
    :param params:
    :return:
    """

    if not os.path.exists(config.features_dir):
        os.makedirs(config.features_dir)
    if config.datainfo_path and os.path.exists(config.datainfo_path):
        with open(config.datainfo_path) as f:
            datainfo = json.load(f)

    # Read the video list and let the videos sort by id in ascending order
    if dataset_name == 'MSVD':
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir))]
        map_file = open(config.mapping_path, 'w')
    elif dataset_name == 'MSR-VTT':
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir), key=lambda x: int(x[5:-4]))]
    elif dataset_name == 'M-VAD':
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir), key=lambda x: int(x[5:-4]))]
    elif dataset_name == 'TRECVID-2020':
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir), key=lambda x: int(x[6:x.index('.')]))]
    elif dataset_name == 'TRECVID-2020-Test':
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir), key=lambda x: int(x[:x.index('.')]))]
    elif dataset_name == 'TGIF':
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir), key=lambda x: int(x[:-4]))]
    elif dataset_name == 'VATEX':
        with open(os.path.join(config.data_dir, 'list.txt')) as f:
            videos = []
            for line in f.readlines():
                path = os.path.join(config.data_dir, line.strip()[1:])
                ss = int(line.rsplit('.',1)[0].split('_')[-2])
                to = int(line.rsplit('.',1)[0].split('_')[-1])
                videos.append((path, (ss, to)))
    elif dataset_name == 'ActivityNet':
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir)) if v.rsplit('.', 1)[0] in datainfo]
        map_file = open(config.mapping_path, 'w')
    elif dataset_name == 'ActivityNet-Test':
        with open(config.test_vid_list_json) as f:
            test_vids = json.load(f)
        videos = [os.path.join(config.data_dir, v) for v in sorted(os.listdir(config.data_dir)) if v.rsplit('.', 1)[0] in test_vids]
        map_file = open(config.mapping_path, 'w')
    elif dataset_name == 'ActivityNet-Fragments':
        videos = []
        for v in sorted(os.listdir(config.data_dir)):
            v_path = os.path.join(config.data_dir, v)
            vid = v.rsplit('.', 1)[0]
            if vid in datainfo:
                for i, ts in enumerate(datainfo[vid]['timestamps']):
                    videos.append((v_path, ts, i))
        map_file = open(config.mapping_path, 'w')         
    elif os.path.exists(os.path.join(config.data_dir, 'list.txt')):
        with open(os.path.join(config.data_dir, 'list.txt')) as f:
            videos = [os.path.join(config.data_dir, path.strip()) for path in f.readlines()]

    # Create an hdf5 file that saves video features
    feature_h5_path = os.path.join(config.features_dir, file_name)
    if os.path.exists(feature_h5_path):
        # If the hdf5 file already exists, it has been processed before,
        # perhaps it has not been completely processed.
        # Read using r+ (read and write) mode to avoid overwriting previously saved data
        h5 = h5py.File(feature_h5_path, 'r+')
    else:
        h5 = h5py.File(feature_h5_path, 'w')

    if dataset_name in list(h5.keys()):
        dataset = h5[dataset_name]
        if extractor_name in list(dataset.keys()):
            dataset_model = dataset[extractor_name]
        elif extractor_name.split('_')[-1] == 'features':
            dataset_model = dataset.create_dataset(extractor_name, (len(videos), config.max_frames, feature_size), dtype='float32')
        elif extractor_name.split('_')[-1] == 'globals': 
            dataset_model = dataset.create_dataset(extractor_name, (len(videos), 1, feature_size), dtype='float32')
        elif extractor_name.split('_')[-1] == 'mask':
            dataset_model = dataset.create_dataset(extractor_name, (len(videos), config.max_frames), dtype='float32')
        dataset_f_ts = dataset['frames_tstamp']
        dataset_counts = dataset['count_features']
    else:
        dataset = h5.create_group(dataset_name)
        if extractor_name.split('_')[-1] == 'features':
            dataset_model = dataset.create_dataset(extractor_name, (len(videos), config.max_frames, feature_size), dtype='float32')
        elif extractor_name.split('_')[-1] == 'globals':
            dataset_model = dataset.create_dataset(extractor_name, (len(videos), 1, feature_size), dtype='float32')
        elif extractor_name.split('_')[-1] == 'mask':
            dataset_model = dataset.create_dataset(extractor_name, (len(videos), config.max_frames), dtype='float32')
        dataset_f_ts = dataset.create_dataset('frames_tstamp', (len(videos), config.max_frames), dtype='float32')
        dataset_counts = dataset.create_dataset('count_features', (len(videos),), dtype='int')

    if extractor is not None:
        extractor.to(device)
        extractor.eval()
   
    i, forget_idxs = 0, []
    for video_path in videos:
        fragment, all_fragments = None, None
        if dataset_name == 'ActivityNet':
            vid = video_path.split('/')[-1].rsplit('.', 1)[0]
            if vid in datainfo:
                all_fragments = datainfo[vid]['timestamps']
        elif dataset_name == 'ActivityNet-Fragments':
            video_path, fragment, fragment_idx = video_path
        elif dataset_name == 'VATEX':
            video_path, fragment = video_path

        # Extract video frames and video tiles
        sample_type = 'fixed' if extractor_name == 'eco_globals' else 'dynamic'
        max_frames = 16 if extractor_name == 'eco_globals' else config.max_frames
        frame_list, frame_ts_list, clip_list, frame_count, fragments_mask = sample_frames(sample_type, video_path, max_frames,
                                                                                          config.frame_sample_rate,
                                                                                          config.clip_size, 
                                                                                          segment_secs=fragment,
                                                                                          all_fragments=all_fragments)
#         sampled_frames, frame_count = sample_frames2(video_path, num_segments=16, segment_length=1)
        if frame_count == 0:
            print('The ', i,' video: ', video_path, ' was discarded because it does not have correct frames.')
            forget_idxs.append(i)
            if dataset_name in ['TRECVID-2020', 'VATEX', 'TGIF']:
                i+=1
            continue
        if frame_count < config.clip_size:
            print('The ', i,' video: ', video_path, ' was discarded because it has less than ', config.clip_size, ' correct frames.')
            forget_idxs.append(i)
            if dataset_name in ['TRECVID-2020', 'VATEX', 'TGIF']:
                i+=1
            continue

        if dataset_name == 'MSVD':
            map_name_id = '{}\tvideo{}\n'.format(video_path.split('/')[-1].rsplit('.', 1)[0], i)
            map_file.write(map_name_id)
        elif dataset_name == 'ActivityNet':
            vid = video_path.split('/')[-1].rsplit('.', 1)[0]
            if vid in datainfo:
                all_fragments = datainfo[vid]['timestamps']
            map_name_id = '{} \t {}\n'.format(vid, i)
            map_file.write(map_name_id)
        elif dataset_name == 'ActivityNet-Test':
            vid = video_path.split('/')[-1].rsplit('.', 1)[0]
            map_name_id = '{} \t {}\n'.format(vid, i)
            map_file.write(map_name_id)
        elif dataset_name == 'ActivityNet-Fragments':
            map_name_timestamp_idx = '{} \t {} \t {} \t {} \t {}\n'.format(video_path.split('/')[-1].rsplit('.', 1)[0], fragment[0], fragment[1], fragment_idx, i)
            map_file.write(map_name_timestamp_idx)

        if i % 50 == 0:
            if fragment is None:
                print('%d\t%s\t%d' % (i, video_path.split('/')[-1], frame_count))
            else:
                print('%d\t%s\t[%f,%f]\t%d' % (i, video_path.split('/')[-1], fragment[0], fragment[1], frame_count))

        if extractor_name == 'events_mask':    
            assert fragments_mask is not None, 'to compute the mask you must pass the list of fragments to the sample_frames method'
            features = torch.zeros(1, config.max_frames)
            features[0,:len(fragments_mask)] = torch.tensor(fragments_mask)
        elif extractor_name == 'cnn_features': 
            # Preprocess frames and then convert it into (batch, channel, height, width) format
#             frame_list = np.array([preprocess_frame(x, scale_size=scale_size, crop_size=crop_size,
#                                                     mean=input_mean, std=input_std, normalize_input=True) 
#                                    for x in frame_list])
#             frame_list = torch.from_numpy(frame_list.transpose((0, 3, 1, 2))).to(device)
#             frame_list = torch.cat([preprocess_frame(x, scale_size=scale_size, crop_size=crop_size,
#                                                     mean=input_mean, std=input_std, normalize_input=True).unsqueeze(0) 
#                                    for x in frame_list], dim=0).to(device)
            frame_list = torch.cat([transformer(x).unsqueeze(0) for x in frame_list], dim=0).to(device)

            # Extracting cnn features of sampled frames first
            features = extractor(frame_list)
            print(extractor_name, i, features.size(), features.mean())
        elif extractor_name in ['cnn_globals', 'cnn_sem_globals']:
            frame_list = torch.cat([transformer(x).unsqueeze(0) for x in frame_list], dim=0).to(device)
            features = extractor(frame_list.transpose(0,1).unsqueeze(0))
            print(frame_list.size(), features.size(), features.min(), features.max(), features.mean())
        elif extractor_name in ['c3d_features', 'i3d_features']:
            # Preprocess frames of the video fragments to extract motion features
#             clip_list = np.array([[preprocess_frame(x, scale_size=extractor.scale_size, crop_size=extractor.crop_size,
#                                                     mean=extractor.input_mean, std=extractor.input_std) for x in clip] for clip in clip_list])
#             clip_list = clip_list.transpose((0, 4, 1, 2, 3)).astype(np.float32)
#             clip_list = torch.from_numpy(clip_list).to(device)

            b, n, features = 70, len(clip_list), []
            for c in clip_list:
                assert config.clip_size == len(c), '{}!={}'.format(config.clip_size, len(c))
            for j in range(0, n, b):
                clips_batch = [torch.cat([transformer(x).unsqueeze(0) for x in c], dim=0).unsqueeze(0) for c in clip_list[j:min(j+b, n)]]
                clips_batch = torch.cat(clips_batch, dim=0).transpose(1,2).to(device)

                # Extracting c3d features
                features.append(extractor(clips_batch))
            features = torch.cat(features, dim=0)
            print(features.size(), features.mean())
        elif extractor_name in ['c3d_globals', 'i3d_globals']:
            # Preprocess frames of the video fragments to extract motion features
            frames = np.array([preprocess_frame(x, scale_size=scale_size, crop_size=crop_size,
                                                mean=input_mean, std=input_std) for x in frame_list])
            frames = torch.from_numpy(frames.transpose((0, 3, 1, 2)).astype(np.float32)).unsqueeze(2).to(device)

            # Extracting i3d features of sampled frames first
            features = extractor(frames)[1]
            print(features.size(), features.mean())
        elif extractor_name in ['eco_features', 'tsm_features', 'eco_sem_features', 'tsm_sem_features']:
            features = []
            for clip in clip_list:
                clip_frames = torch.cat([torch.from_numpy(preprocess_frame(x, scale_size=scale_size, crop_size=crop_size,
                                                                          mean=input_mean, std=input_std))
                                         for x in clip], dim=2).transpose(0,2).unsqueeze(0).to(device)
                # Extracting eco features from current clip
                features.append(extractor(clip_frames))
            features = torch.cat(features, dim=0)
            if extractor_name in ['eco_sem_features', 'tsm_sem_features']:
                probs = torch.softmax(features, dim=1)
                print(probs.size(), probs.max(), probs.min())
        elif extractor_name in ['eco_globals', 'tsm_globals', 'eco_sem_globals', 'tsm_sem_globals']:
            frames = torch.cat([torch.from_numpy(preprocess_frame(x, crop_size=224, scale_size=256, mean=[104, 117, 128], std=[1]))
                                 for x in frame_list], dim=2).transpose(0,2).unsqueeze(0)

            # Extracting eco-semantic features from sampled frames
            features = extractor(frames.to(device)).squeeze(0)
            print(features.size(), features.min(), features.max(), features.mean())
            
            if extractor_name in ['eco_sem_globals', 'tsm_sem_globals']:
                probs = torch.softmax(features, dim=1)
                print(probs.size(), probs.max(), probs.min())

        if extractor_name.split('_')[-1] == 'features':
            dataset_model[i] = np.zeros((config.max_frames, feature_size), dtype='float32')
            dataset_model[i, :features.size(0), :] = features.data.cpu().numpy()
            dataset_counts[i] = features.size(0)
            dataset_f_ts[i] = np.zeros((config.max_frames), dtype='float32')
            dataset_f_ts[i, :features.size(0)] = frame_ts_list
        else:
            dataset_model[i] = features.data.cpu().numpy()
            
        i+=1

    h5.close()

    print('discarded idxs: ', forget_idxs)

    if dataset_name in ['MSVD','ActivityNet', 'ActivityNet-Test','ActivityNet-Fragments']:
        map_file.close()


def main(args, config):   
    if torch.cuda.is_available():
        gpu_id = get_freer_gpu()[0]
        device = torch.device('cuda:{}'.format(gpu_id))
        torch.cuda.set_device('cuda:{}'.format(gpu_id))
        print(f'Torch current device: cuda:{torch.cuda.current_device()}')
        print(f'Running on freer device: cuda:{gpu_id}')
    else:
        device = torch.device('cpu')
        print('Running on cpu device')
    
    file_name = '{}_features_linspace{}_{}-{}.h5'.format(config.dataset_name.lower(), config.frame_sample_rate, config.max_frames, '-'.join(args.features))

    for feats_name in args.features:
        if feats_name == 'events_mask':
            extract_features(file_name, feats_name, None, args.dataset_name, device, config, None, None)
        if feats_name == 'cnn_features':
            print('Extracting CNN for {} dataset'.format(args.dataset_name))
            cnn_use_torch_weights = (config.cnn_pretrained_path == '')
            model = CNN(config.cnn_model, input_size=224, use_pretrained=cnn_use_torch_weights, use_my_resnet=False)
            if not cnn_use_torch_weights:
                model.load_pretrained(config.cnn_pretrained_path)
            transformer = transforms.Compose([transforms.Scale(model.scale_size),
                                            transforms.CenterCrop(model.crop_size),
                                            transforms.ToTensor(),
                                            transforms.Normalize(mean=model.input_mean, std=model.input_std)])
            with torch.no_grad():
                extract_features(file_name, feats_name, model, args.dataset_name, device, config, model.feature_size, transformer)
        if feats_name in ['cnn_globals', 'cnn_sem_globals']:
            print('Extracting ResNet (2+1)D for {} dataset'.format(args.dataset_name))
            model = CNN('r2plus1d_18', input_size=112, use_pretrained=True, use_my_resnet=False, get_probs=feats_name=='cnn_sem_globals')
            transformer = transforms.Compose([transforms.Resize((128, 171)),
                                            transforms.CenterCrop((112, 112)),
                                            transforms.ToTensor(),
                                            transforms.Normalize(mean=[0.43216, 0.394666, 0.37645],
                                                                std=[0.22803, 0.22145, 0.216989]),
                                            ])
            with torch.no_grad():
                extract_features(file_name, feats_name, model, args.dataset_name, device, config, model.feature_size, transformer)
        if feats_name in ['c3d_features', 'c3d_globals']:
            print('Extracting C3D for {} dataset'.format(args.dataset_name))
            model = C3D()
            model.load_pretrained(config.c3d_pretrained_path)
            transformer = transforms.Compose([transforms.Scale((200, 112)),
                                            transforms.CenterCrop(112),
                                            ToTensorWithoutScaling(),
                                            transforms.Normalize(mean=model.input_mean, std=model.input_std)])
            with torch.no_grad():
                extract_features(file_name, feats_name, model, args.dataset_name, device, config, model.feature_size, transformer)
        if feats_name in ['c3d_globals', 'i3d_globals']:
            print('Extracting I3D for {} dataset'.format(args.dataset_name))
            model = I3D(modality='rgb')
            model.load_state_dict(torch.load(config.i3d_pretrained_path))
            with torch.no_grad():
                extract_features(file_name, feats_name, model, args.dataset_name, device, config, feature_size=model.feature_size,
                              crop_size=model.crop_size, scale_size=model.scale_size, input_mean=model.input_mean, 
                              input_std=model.input_std)
        if feats_name in ['eco_features', 'eco_globals']:
            print('Extracting ECOfull for {} dataset'.format(args.dataset_name))
            model, crop_size, scale_size, input_mean, input_std = ECO(num_class=400, num_segments=16, 
                                                                    pretrained_parts='finetune', #'2D', '3D',
                                                                    modality='RGB', 
                                                                    arch='ECOfull',  # 'ECOfull' 'ECO' 'C3DRes18'
                                                                    consensus_type='identity', #'avg',
                                                                    dropout=0, 
                                                                    no_partialbn=True, #False, 
                                                                    resume_chpt='',
                                                                    get_global_pool=True,
                                                                    gpus=[device])
            transformer = transforms.Compose([transforms.Scale((scale_size, scale_size)),
                                            transforms.CenterCrop(crop_size),
                                            ToTensorWithoutScaling(),
                                            transforms.Normalize(mean=input_mean, std=input_std)])
            with torch.no_grad():
                extract_features(file_name, feats_name, model, args.dataset_name, device, config, 1536, transformer)
        if feats_name in ['eco_sem_features', 'eco_sem_globals']:
            print('Extracting ECO-Smantic for {} dataset'.format(args.dataset_name))
            model, crop_size, scale_size, input_mean, input_std = ECO(num_class=400, num_segments=config.frame_sample_rate, 
                                                                    pretrained_parts='finetune', #'2D', '3D',
                                                                    modality='RGB', 
                                                                    arch='ECOfull',  # 'ECOfull' 'ECO' 'C3DRes18'
                                                                    consensus_type='identity', #'avg',
                                                                    dropout=.8, 
                                                                    no_partialbn=True, #False, 
                                                                    resume_chpt='',
                                                                    get_global_pool=False,
                                                                    gpus=[device])
            with torch.no_grad():
                extract_features(file_name, 'eco_sem_features', model, args.dataset_name, device, config, 400, crop_size, scale_size,
                                 input_mean, input_std)
        if feats_name in ['tsm_features', 'tsm_globals']:
            print('Extracting {} for {} dataset'.format(feats_name, args.dataset_name))
            model, crop_size, scale_size, input_mean, input_std = TSM(num_class=174, num_segments=config.frame_sample_rate, 
                                                                    modality='RGB', 
                                                                    arch='resnet50',  # 'resnet101'
                                                                    consensus_type='avg', # 'avg' 'identity'
                                                                    dropout=0, 
                                                                    img_feature_dim=256,
                                                                    no_partialbn=True, #False,
                                                                    pretrain='imagenet',
                                                                    is_shift=True, 
                                                                    shift_div=8, 
                                                                    shift_place='blockers', 
                                                                    non_local=False,
                                                                    temporal_pool=False,
                                                                    resume_chkpt='./models/Smth-Smth-v2-tsm/TSM_somethingv2_RGB_resnet50_shift8_blockres_avg_segment16_e45.pth',
                                                                    get_global_pool=True,
                                                                    gpus=[device])
        if feats_name in ['tsm_sem_features', 'tsm_sem_globals']:
            print('Extracting {} for {} dataset'.format(feats_name, args.dataset_name))
            model, crop_size, scale_size, input_mean, input_std = TSM(num_class=174, num_segments=config.frame_sample_rate, 
                                                                    modality='RGB', 
                                                                    arch='resnet50',  # 'resnet101'
                                                                    consensus_type='avg', # 'avg' 'identity'
                                                                    dropout=.8, 
                                                                    img_feature_dim=256,
                                                                    no_partialbn=True, #False,
                                                                    pretrain='imagenet',
                                                                    is_shift=True, 
                                                                    shift_div=8, 
                                                                    shift_place='blockers', 
                                                                    non_local=False,
                                                                    temporal_pool=False,
                                                                    resume_chkpt='./models/Smth-Smth-v2-tsm/TSM_somethingv2_RGB_resnet50_shift8_blockres_avg_segment16_e45.pth',
                                                                    get_global_pool=False,
                                                                    gpus=[device])
            with torch.no_grad():
                extract_features(file_name, feats_name, model, args.dataset_name, device, config, 174, crop_size, scale_size, input_mean, input_std)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train a captioning model for a specific dataset.')
    parser.add_argument('-ds', '--dataset_name', type=str, default='MSVD',
                        help='Set The name of the dataset (default is MSVD).')
    parser.add_argument('-f','--features', nargs='+', 
                        help='<Required> Set the names of features to be extracted', required=True)
    parser.add_argument('-config', '--config_file', type=str, required=True,
                        help='<Required> Set the path to the config file with other configuration params')

    args = parser.parse_args()

    assert args.dataset_name in ['MSVD', 'M-VAD', 'MSR-VTT', 'TRECVID-2020', 'TRECVID-2020-Test', 'TGIF', 'VATEX', 'ActivityNet', 'ActivityNet-Test', 'ActivityNet-Fragments']
    for f_name in args.features:
      assert f_name in ['events_mask', 'cnn_features', 'cnn_globals', 'cnn_sem_globals', 'c3d_features', 'c3d_globals', 'i3d_features', 'i3d_globals', 'eco_features', 'eco_globals', 'eco_sem_features', 'eco_sem_globals', 'tsm_sem_features', 'tsm_sem_globals', 'tsm_features', 'tsm_globals']
    
    config = ConfigurationFile(args.config_file, args.dataset_name)

#     while True:
#         try:
    main(args, config)
    print('Extraction of all features finished!!')
#             break
#         except OSError:
#             time.sleep(10)
#             print('\ntrying again...')