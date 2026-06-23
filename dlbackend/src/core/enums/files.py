from enum import StrEnum


class ModelEnum(StrEnum):
    # Audio embedder (WeSpeaker)
    WESPEAKER_RESNET34 = "wespeaker_resnet34"
    WESPEAKER_ECAPA_TDNN_1024 = "wespeaker_ecapa_tdnn1024"
    WESPEAKER_CAMPPLUS = "wespeaker_campplus"

    # Audio emotion (SER)
    EMOTION2VEC_ONNX = "emotion2vec_onnx"

    # Facial emotion (FER)
    POSTERV2_ONNX = "posterv2_onnx"
    POSTERV2_PTH = "posterv2_pth"
    EMONET_8_ONNX = "emonet_8_onnx"
    EMONET_8_PTH = "emonet_8_pth"
    EMONET_5_ONNX = "emonet_5_onnx"
    EMONET_5_PTH = "emonet_5_pth"

    # Action recognition
    X3D_ONNX = "x3d_onnx"
    VIDEOMAE_ONNX = "videomae_onnx"
    UNIFORMERV2_ONNX = "uniformerv2_onnx"
    UNIFORMERV2_PTH = "uniformerv2_pth"

    # Pose 2D estimation
    RTMPOSE_M_ONNX = "rtmpose_m_onnx"

    # Pose 3D lifting
    TCPFORMER_H36M_243_ONNX = "tcpformer_h36m_243_onnx"
    TCPFORMER_H36M_243_PTH = "tcpformer_h36m_243_pth"

    # Face detection
    YUNET_ONNX = "yunet_onnx"

    # Person detection
    YOLO_PERSON_ONNX = "yolo_person_onnx"
    YOLO_PERSON_NMS_ONNX = "yolo_person_nms_onnx"
    YOLO_PERSON_PTH = "yolo_person_pth"

    # Object detection
    YOLO_WORLD_ONNX = "yolo_world_onnx"
    YOLO_WORLD_NMS_ONNX = "yolo_world_nms_onnx"
    YOLO_WORLD_PTH = "yolo_world_pth"
    OWLV2_ONNX = "owlv2_onnx"
    OWLV2_NMS_ONNX = "owlv2_nms_onnx"
