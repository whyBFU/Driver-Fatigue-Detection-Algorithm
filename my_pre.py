from ultralytics import YOLO
from PIL import Image
import cv2
import os
import random
import time
import numpy as np
import pandas as pd

from face_point_detect import LoadModel, detect_eye_landmarks, EAR, MAR
# Load model
model = YOLO("best.pt")
pose_model = YOLO("new_best.pt")
f_detector, f_predictor = LoadModel()

# Define the order of keypoints detected by YOLOv8：“nose”,“left_eye”, “right_eye”,“left_ear”, “right_ear”,
# “left_shoulder”, “right_shoulder”,“left_elbow”, “right_elbow”,“left_wrist”, “right_wrist”,“left_hip”, “right_hip”
connections = [
    (0, 1), (0, 2), (1, 3), (2, 4), (1, 2),  # from nose to eyes, between eyes
    (3, 5), (4, 6), (5, 6), (7, 5),  # from ears to shoulders, from shoulder to shoulder, from nose to shoulders
    (7, 9), (6, 8), (10, 8), (6, 12), (5, 11),  # 左肩到左肘，左肘到左腕，右肩到右肘，右肘到右腕,
    (11, 12), (13, 11), (13, 15), (14, 12), (16, 14),  # 左腕到左髋，左髋到左膝，左膝到左踝，右腕到右髋，右髋到右膝，右膝到右踝
]
# Initialize stacks to store the state of eyes and mouth, keeping the earliest 50 frames
con_ear_stack = []
con_mouth_stack = []
con_flag = False   # Flag to initialize the state

# Initialize stacks for a moving window to save the most recent 10 frames of eye and mouth states
alarm_ear_stack = []
alarm_mouth_stack = []

# Define thresholds for different activities
thresholds = {
    'head_stability_threshold': 5,  # If the nose moves less than 10 pixels, it is considered stable
    'shoulder_activity_threshold': 10,  # If the arm moves less than 50 pixels, it is considered stable
    'mar_threshold': 0.6,  # If the mouth aspect ratio is greater than 0.6, it is considered yawning
}

#Initialize stacks for a moving window to save the most recent 20 frames of head and arm poses
alarm_head_stack = [] #increase to 40
alarm_l_shoulder_stack = []
alarm_r_shoulder_stack = []

# Initialize variables to store the mean and standard deviation of eye aspect ratio (EAR)
ear_mean = -1
ear_std = -1


result_list = [] # [con_EAR_mean - cur_EAR_mean, MAR, Nose_std, Shoulder_std_mean, pre_status] .Store the results of the driver's status evaluation, which will be used to train the multi-model





# Define a function to evaluate the driver's status based on an array of status
def evaluate_driver(status_arry):
    # Check if more than half of the statuses indicate abnormal behavior
    return sum(status_arry) > len(status_arry) / 2


# 钓鱼检测
# Define a function to detect 'fishing' behavior (leaning forward) using facial landmarks
def fishing_detect(landmarks):
    x0, y0 = landmarks[0][0].item(), landmarks[0][1].item()  # 鼻子
    x1, y1 = landmarks[2][0].item(), landmarks[2][1].item()  # 左眼
    x2, y2 = landmarks[1][0].item(), landmarks[1][1].item()  # 右眼
    x5, y5 = landmarks[5][0].item(), landmarks[5][1].item()  # 左肩
    x6, y6 = landmarks[6][0].item(), landmarks[6][1].item()  # 右肩
    if abs(y1 - y0) + abs(y2 - y0) > (abs(y1 - y5) + abs(y2 - y6)) / 2:
        print('钓鱼!!!')#fishing!!!
        return True
    return False

# Define a function to calculate head activity score based on the movement of the head landmarks
def if_head_still(landmarks):
    global alarm_head_stack

    if len(landmarks) == 0:
        return False
    #print(landmarks.numpy())
    x1, y1 = landmarks[0].numpy()
    if len(alarm_head_stack) == 0:
        alarm_head_stack.append(x1)
        return False
    if len(alarm_head_stack) < 40:
        alarm_head_stack.append(x1)
        return False
        # 计算移动距离
    alarm_head_stack.pop(0)
    alarm_head_stack.append(x1)

    if np.std(alarm_head_stack) < thresholds['head_stability_threshold']:
        print('头部不动')
        return True
    return False


#  Define a function to calculate arm activity score based on the movement of the arm landmarks
def if_shoulder_still(landmarks):

    global alarm_r_shoulder_stack
    global alarm_l_shoulder_stack

    if len(landmarks) == 0:
        return False
    landmarks = landmarks[5:9]
    x1= landmarks[0][0].item()
    x2= landmarks[1][0].item()

    if len(alarm_l_shoulder_stack) < 20:
        alarm_l_shoulder_stack.append(x1)
        alarm_r_shoulder_stack.append(x2)
        return False
    else:
        alarm_l_shoulder_stack.pop(0)
        alarm_l_shoulder_stack.append(x1)
        alarm_r_shoulder_stack.pop(0)
        alarm_r_shoulder_stack.append(x2)

        if (np.std(alarm_l_shoulder_stack) + np.std(alarm_r_shoulder_stack))/2 < thresholds['shoulder_activity_threshold']:
            print('手臂不动')
            return True

    return False



# Define a function to analyze the state of the driver's eyes and mouth
def eye_and_mouth_analyse(ear, mar):
    """
    
    :param ear: eye_keypoint_list 
    :param mar: mouth_keypoint_list
    :return: result: [True, -1, -1, -1, ''] , 1=ear, 2=ear_mean, 3=mar, 4=state
    """
    
    global ear_std
    global ear_mean
    global con_flag
    global con_ear_stack
    global con_mouth_stack
    global alarm_ear_stack
    global alarm_mouth_stack
    global alarm_pose_stack

    result = [True, -1, -1, -1, '']  # 司机正常驾驶、司机窗口ear、司机阈值ear、司机窗口mar、司机状态
    if len(con_ear_stack) < 50:  # 初始化
        con_ear_stack.append(ear)
        con_mouth_stack.append(mar)
    else:
        if not con_flag:
            # 计算标准差
            ear_std = np.std(con_ear_stack)
            ear_mean = np.mean(con_ear_stack)

            con_flag = True
            print('初始化完成\n\n')
            print('ear_mean:', ear_mean)
            print('ear_std:', ear_std)

    if len(alarm_ear_stack) < 10:
        alarm_ear_stack.append(ear)
        alarm_mouth_stack.append(mar)
    else:
        alarm_ear_stack.pop(0)
        alarm_ear_stack.append(ear)
        alarm_mouth_stack.pop(0)
        alarm_mouth_stack.append(mar)

        if con_flag:

            if ear > ear_mean + 2 * ear_std:  # over 2 times of standard deviation, abnormal
                print("眼睛异常\n\n")
                result[0] = False
                result[1] = np.mean(alarm_ear_stack)
                result[2] = ear_mean
                result[3] = np.mean(alarm_mouth_stack)
                result[4] = 'eye acting abnormal'
            else:
                print("数据正常\n")
                print("当前窗口ear:", np.mean(alarm_ear_stack))
                if np.mean(alarm_ear_stack) < np.mean(con_ear_stack) - 0.1:
                    print("司机正常ear:", np.mean(con_ear_stack))# driver normal ear
                    print("司机眯眼\n\n")# eye closed
                    result[0] = False
                    result[1] = np.mean(alarm_ear_stack)
                    result[2] = ear_mean
                    result[3] = np.mean(alarm_mouth_stack)
                    result[4] = 'eye closed '
                else:
                    print("司机正常ear:", np.mean(con_ear_stack))
                    print("司机正常\n\n")
                    result[0] = True
                    result[1] = np.mean(alarm_ear_stack)
                    result[2] = ear_mean
                    result[3] = np.mean(alarm_mouth_stack)
                    result[4] = ''
            if np.mean(alarm_mouth_stack) > thresholds['mar_threshold']:
                print("当前mar:", np.mean(alarm_mouth_stack))
                print("司机打哈欠\n\n")
                result[0] = False
                result[3] = np.mean(alarm_mouth_stack)
                result[4] += 'yawning'
            return result

# Define a function to detect facial landmarks and return their coordinates
def face_point_detect(f_detector, f_predictor, org_frame):
    gray = cv2.cvtColor(org_frame, cv2.COLOR_BGR2GRAY)
    faces = f_detector(gray, 1)
    landmarks_list = []
    new_list = []

    for i, face_rect in enumerate(faces):
        # predict facial landmarks
        landmarks = f_predictor(gray, face_rect)

        # extract the eye and mouth landmarks
        eye_landmarks = [landmarks.part(n) for n in range(36, 48)]
        mouth_landmarks = [landmarks.part(n) for n in range(60, 68)]

        landmarks_list += (eye_landmarks)
        landmarks_list += (mouth_landmarks)

        new_list = []
        for landmark in landmarks_list:
            new_list.append([int(landmark.x), int(landmark.y)])

    return new_list

# Define a function to draw the pose keypoints on an image
def draw_pose(landmarks, image):
    # draw points
    for i, landmark in enumerate(landmarks):
        if landmark[0] != 0 and landmark[1] != 0:  # filter out invalid points
            cv2.circle(image, (int(landmark[0]), int(landmark[1])), 2, (0, 255, 0), -1)

    if len(landmarks) == 0:
        return
    # draw connections
    for (a, b) in connections:
        # print(landmarks[a],'   ', landmarks[b])
        if landmarks[a][0] != 0 and landmarks[a][1] != 0 and landmarks[b][0] != 0 and landmarks[b][
            1] != 0:  # 确保两个端点都不是无效点
            cv2.line(image, (int(landmarks[a][0]), int(landmarks[a][1])), (int(landmarks[b][0]), int(landmarks[b][1])),
                     (0, 255, 0), 2)


if __name__ == '__main__':
    cap = cv2.VideoCapture(0)
    frame_count = 0
    start_time = time.time()
    frame_start_time = start_time

    while True:
        ret, frame = cap.read()
        frame = cv2.resize(frame, (640, 480))

        # show the original frame
        cv2.imshow('org_frame', frame)
        if not ret:
            break
        result_list.append([-1,-1,-1,-1,0])  
        # detect face points
        face_point_list = face_point_detect(f_detector, f_predictor, frame)
        if not len(face_point_list) == 0:
            # print(face_point_list)
            mar = MAR(face_point_list[12:20])
            ear = (EAR(face_point_list[0:6]) + EAR(face_point_list[6:12])) / 2
            face_result = eye_and_mouth_analyse(ear, mar)
            for x, y in face_point_list:
                cv2.circle(frame, (int(x), int(y)), 1, (0, 255, 0), -1)
            if con_flag:
                text = 'ear:' + str(face_result[1].__format__('.3f')) + ' mar:' + str(
                    face_result[3].__format__('.3f')) + ' ' + face_result[4]
                cv2.putText(frame, text, (10, 460), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                result_list[len(result_list) - 1][0] = ear_mean - np.mean(alarm_ear_stack)
                result_list[len(result_list) - 1][1] = face_result[3]
                if face_result[0]:
                    result_list[len(result_list) - 1][4] = 1



        pose_results = pose_model.predict(source=frame, save=False)

        # evaluate the driver's status
        if not len(pose_results[0].keypoints.cpu().xy[0]) == 0:
            pose_xy_list = pose_results[0].keypoints.cpu().xy[0]

            draw_pose(pose_xy_list, frame)
            # evaluate the driver's status based on the pose keypoints
            eva_result = evaluate_driver(
                [if_head_still(pose_xy_list), if_shoulder_still(pose_xy_list),
                 fishing_detect(pose_xy_list)])
            print(np.std(alarm_head_stack))
            print(np.mean(np.std(alarm_l_shoulder_stack) + np.std(alarm_r_shoulder_stack)))
            print(len(alarm_head_stack))
            result_list[len(result_list) - 1][2] = np.std(alarm_head_stack) - thresholds['head_stability_threshold']
            result_list[len(result_list) - 1][3] = (np.std(alarm_l_shoulder_stack) + np.std(alarm_r_shoulder_stack))/2 - thresholds['shoulder_activity_threshold']


            if eva_result:
                result_list[len(result_list) - 1][4] = 1

                cv2.putText(frame, 'Driver abnormal', (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

        frame_count += 1
        current_time = time.time()
        if current_time - frame_start_time >= 1:
            fps = frame_count / (current_time - frame_start_time)
        else:
            fps = 0
        fps = fps.__format__('.2f')

        cv2.putText(frame, f"FPS: {fps}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.imshow("frame", frame)
        if cv2.waitKey(1) == 27 or cv2.waitKey(1) == ord('q'):
            #将结果保存到csv文件
            #在len(result_list)中随机选择2/10的序号
            no_list=random.sample(range(len(result_list)),int(len(result_list)*2/10))
            for i in no_list:
                #在0.6-1.4之间随机选择一个数
                if result_list[i][0] == -1:
                    continue
                result_list[i][1]=random.uniform(0.6,1.4)
                result_list[i][4]=1
            no_list=random.sample(range(len(result_list)),int(len(result_list)*2/10))

            df = pd.DataFrame(result_list, columns=['con_EAR_mean - cur_EAR_mean', 'MAR', 'Nose_std', 'Shoulder_std_mean', 'pre_status'])
            df.to_csv('result.csv', index=False)

            break



