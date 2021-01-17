import os
import sys
import cv2
import torch
import argparse
import numpy as np
from tqdm import tqdm
from torch.nn import functional as F
import warnings
import _thread
import skvideo.io
from queue import Queue, Empty
warnings.filterwarnings("ignore")

from pprint import pprint
import time
import psutil

import multiprocessing as mp

# Exception handler
def exeption_handler(exctype, value, tb):
    import traceback

    locks = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'locks')
    cmd = 'rm -f ' + locks + '/*'
    os.system(cmd)

    pprint ('%s in %s' % (value, exctype))
    pprint(traceback.format_exception(exctype, value, tb))
    sys.__excepthook__(exctype, value, tb)
    input("Press Enter to continue...")
sys.excepthook = exeption_handler

# ctrl+c handler
import signal
def signal_handler(sig, frame):
    global ThreadsFlag
    ThreadsFlag = False
    time.sleep(0.1)
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

def worker(num):
    print ('Worker:', num)
    return

def clear_write_buffer(user_args, write_buffer, tot_frame):
    new_frames_number = ((tot_frame - 1) * ((2 ** args.exp) -1)) + tot_frame
    print ('rendering %s frames to %s/' % (new_frames_number, args.output))
    pbar = tqdm(total=new_frames_number, unit='frame')
    cnt = 0
    while ThreadsFlag:
        item = write_buffer.get()

        if item is None:
            pbar.close()
            break
        
        if cnt < new_frames_number:
            cv2.imwrite(os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(cnt)), item[:, :, ::-1], [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF])
        pbar.update(1)
        cnt += 1

def find_middle_frame(frames):
    for start_frame in range(1, len(frames.keys())):
        for frame_number in range (start_frame, len(frames.keys())):
            if frames.get(frame_number) and (not frames.get(frame_number + 1)): 
                start_frame = frame_number
                break

        for frame_number in range(start_frame + 1, len(frames.keys())):
            if frames.get(frame_number):
                end_frame = frame_number
                break
            end_frame = frame_number
        
        middle_frame = start_frame + int((end_frame - start_frame) / 2)

        if not frames.get(middle_frame):
            if type(frames.get(middle_frame)) != type(''):
                # this frame is taken by another worker
                continue
            # turn the frame value into int so it marks frame as taken for other workers
            frames[ middle_frame ] = False
            return (start_frame, middle_frame, end_frame)

def three_of_a_perfect_pair(frames, device, padding, model, args, h, w, frames_written):
    perfect_pair = find_middle_frame(frames)
    
    if not perfect_pair:
        print ('no more frames left')
        return False

    start_frame = perfect_pair[0]
    middle_frame = perfect_pair[1]
    end_frame = perfect_pair[2]

    frame0 = cv2.imread(frames[start_frame], cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH)[:, :, ::-1].copy()
    start_frame_out_file_name = os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(start_frame))
    if not os.path.isfile(start_frame_out_file_name):
        cv2.imwrite(os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(start_frame)), frame0[:, :, ::-1], [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF])
        frames_written[ start_frame ] = start_frame_out_file_name
    
    frame1 = cv2.imread(frames[end_frame], cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH)[:, :, ::-1].copy()
    end_frame_out_file_name = os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(end_frame))
    if not os.path.isfile(end_frame_out_file_name):
        cv2.imwrite(os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(end_frame)), frame0[:, :, ::-1], [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF])
        frames_written[ end_frame ] = end_frame_out_file_name


    I0 = torch.from_numpy(np.transpose(frame0, (2,0,1))).to(device, non_blocking=True).unsqueeze(0)
    I1 = torch.from_numpy(np.transpose(frame1, (2,0,1))).to(device, non_blocking=True).unsqueeze(0)
    I0 = F.pad(I0, padding)
    I1 = F.pad(I1, padding)

    diff = (F.interpolate(I0, (16, 16), mode='bilinear', align_corners=False)
        - F.interpolate(I1, (16, 16), mode='bilinear', align_corners=False)).abs()
    
    mid = model.inference(I0, I1, args.UHD)
    mid = (((mid[0]).cpu().detach().numpy().transpose(1, 2, 0)))
    cv2.imwrite(os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(middle_frame)), frame0[:, :, ::-1], [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF])
    frames_written[ middle_frame ] = os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(middle_frame))
    frames[ middle_frame ] = os.path.join(os.path.abspath(args.output), '{:0>7d}.exr'.format(middle_frame))

    return True

if __name__ == '__main__':
    cpus = None
    ThreadsFlag = True
    print('initializing Timewarp ML...')

    parser = argparse.ArgumentParser(description='Interpolation for a pair of images')
    parser.add_argument('--video', dest='video', type=str, default=None)
    parser.add_argument('--input', dest='input', type=str, default=None)
    parser.add_argument('--output', dest='output', type=str, default=None)
    parser.add_argument('--UHD', dest='UHD', action='store_true', help='support 4k video')
    parser.add_argument('--png', dest='png', action='store_true', help='whether to vid_out png format vid_outs')
    parser.add_argument('--ext', dest='ext', type=str, default='mp4', help='vid_out video extension')
    parser.add_argument('--exp', dest='exp', type=int, default=1)

    args = parser.parse_args()
    assert (not args.output is None or not args.input is None)

    manager = mp.Manager()
    frames = manager.dict()
    frames_written = manager.dict()
    img_formats = ['.exr',]
    files_list = []
    for f in os.listdir(args.input):
        name, ext = os.path.splitext(f)
        if ext in img_formats:
            files_list.append(f)

    input_duration = len(files_list)
    first_frame_number = 1
    step = (2 ** args.exp) -1
    last_frame_number = (input_duration - 1) * step + input_duration

    frame_number = first_frame_number
    for file_name in sorted(files_list):
        frames[frame_number] = os.path.join(args.input, file_name)
        frame_number += step + 1

    for frame_number in range(first_frame_number, last_frame_number):
        frames[frame_number] = frames.get(frame_number, '')

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    from model.RIFE_HD import Model
    model = Model()
    model.load_model('./train_log', -1)
    model.eval()
    model.device()
    
    if torch.cuda.is_available():
        torch.set_grad_enabled(False)
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.benchmark = True    

    first_frame = cv2.imread(frames.get(first_frame_number), cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH)[:, :, ::-1].copy()
    h, w, _ = first_frame.shape

    ph = ((h - 1) // 64 + 1) * 64
    pw = ((w - 1) // 64 + 1) * 64
    padding = (0, pw - w, 0, ph - h)

    #write_buffer = Queue(maxsize=500)
    #read_buffer = Queue(maxsize=500)
    #_thread.start_new_thread(build_read_buffer, (args, read_buffer, videogen))
    #_thread.start_new_thread(clear_write_buffer, (args, write_buffer, tot_frame))

    output_folder = os.path.abspath(args.output)
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    max_cpu_workers = mp.cpu_count() - 2
    total_workers_needed = last_frame_number - input_duration
    available_ram = psutil.virtual_memory()[1]/( 1024 ** 3 )
    megapixels = ( h * w ) / ( 10 ** 6 )
    thread_ram = megapixels * 1.92
    sim_workers = int( available_ram / thread_ram )
    if sim_workers < 1:
        sim_workers = 1
    elif sim_workers > max_cpu_workers:
        sim_workers = max_cpu_workers

    print ('---\nAvaliable RAM: %s Gb' % '{0:.2f}'.format(available_ram))
    print ('Estimated memory usage: %s Gb per CPU thread for %sx%s image' % ('{0:.2f}'.format(thread_ram), w, h))
    print ('Issuing %s CPU threads (%s avaliable\n---' % (sim_workers, mp.cpu_count()))
    
    pbar = tqdm(total=last_frame_number, unit='frame')

    active_workers = []

    for i in range (0, total_workers_needed):
        p = mp.Process(target=three_of_a_perfect_pair, args=(frames, device, padding, model, args, h, w, frames_written, ))
        p.start()
        active_workers.append(p)

        while len(active_workers) >= sim_workers:
            finished_workers = []
            alive_workers = []
            for worker in active_workers:
                if not worker.is_alive():
                    finished_workers.append(worker)
                else:
                    alive_workers.append(worker)
            active_workers = list(alive_workers)
            # pbar.n = len(frames_written.keys())
            # pbar.last_print_n = len(frames_written.keys())
            # pbar.refresh()
            time.sleep(0.01)

        # pbar.n = len(frames_written.keys())
        # pbar.last_print_n = len(frames_written.keys())
        # pbar.refresh()

        #for k in range(1, 15):
        #    print ('%s: %s' % (k, frames.get(k)))

    while len(active_workers):
        finished_workers = []
        alive_workers = []
        for worker in active_workers:
            if not worker.is_alive():
                finished_workers.append(worker)
            else:
                alive_workers.append(worker)
        active_workers = list(alive_workers)
        time.sleep(0.01)

    pbar.close()
    input("Press Enter to continue...")
    sys.exit()


            #for local_frame_index in sorted(mp_output.keys()):
            #    write_buffer.put(mp_output[local_frame_index])

        # write_buffer.put(frames[-1])
    '''
    else:

        I1 = torch.from_numpy(np.transpose(lastframe, (2,0,1))).to(device, non_blocking=True).unsqueeze(0)
        I1 = F.pad(I1, padding)
        frame = read_buffer.get()

        for nn in range(1, tot_frame+1):

            frame = read_buffer.get()
            if frame is None:
                break

            I0 = I1
            I1 = torch.from_numpy(np.transpose(frame, (2,0,1))).to(device, non_blocking=True).unsqueeze(0)
            I1 = F.pad(I1, padding)

            diff = (F.interpolate(I0, (16, 16), mode='bilinear', align_corners=False)
                - F.interpolate(I1, (16, 16), mode='bilinear', align_corners=False)).abs()
            
            if diff.mean() > 0.2:
                output = []
                for i in range((2 ** args.exp) - 1):
                    output.append(I0)
            else:
                output = make_inference(model, I0, I1, args.exp, args.UHD)
                
            write_buffer.put(lastframe)
            for mid in output:
                if sys.platform == 'darwin':
                    mid = (((mid[0]).cpu().detach().numpy().transpose(1, 2, 0)))
                else:
                    mid = (((mid[0]).cpu().numpy().transpose(1, 2, 0)))
                write_buffer.put(mid[:h, :w])

            # pbar.update(1)
            lastframe = frame

    write_buffer.put(lastframe)

    while(not write_buffer.empty()):
        time.sleep(0.1)

    # pbar.close()
    if not vid_out is None:
        vid_out.release()
    '''

    import hashlib
    lockfile = os.path.join('locks', hashlib.sha1(output_folder.encode()).hexdigest().upper() + '.lock')
    if os.path.isfile(lockfile):
        os.remove(lockfile)

    # input("Press Enter to continue...")


