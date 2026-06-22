from enum import StrEnum


class ModelEnum(StrEnum):
    # Audio embedder (WeSpeaker)
    WESPEAKER_RESNET34 = "wespeaker_resnet34"
    WESPEAKER_ECAPA_TDNN_1024 = "wespeaker_ecapa_tdnn1024"
    WESPEAKER_CAMPPLUS = "wespeaker_campplus"

    # Audio emotion (SER)
    EMOTION2VEC = "emotion2vec"

    # Facial emotion (FER)
    POSTERV2 = "posterv2"
    EMONET_8 = "emonet_8"
    EMONET_5 = "emonet_5"

    # Action recognition
    X3D = "x3d"
    VIDEOMAE = "videomae"
    UNIFORMERV2 = "uniformerv2"

    # Pose 2D estimation
    RTMPOSE_M = "rtmpose_m"

    # Pose 3D lifting
    TCPFORMER_H36M_243 = "tcpformer_h36m_243"

    # Face detection
    YUNET = "yunet"

    # Person detection
    YOLO_PERSON = "yolo_person"

    # Object detection
    YOLO_WORLD = "yolo_world"
    OWLV2 = "owlv2"
    GROUNDING_DINO = "grounding_dino"
