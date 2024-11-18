from pathlib import Path

class Config:
    # Data
    TRAIN_IMAGE_ROOT = "../data/train/DCM"
    TRAIN_LABEL_ROOT = "../data/train/outputs_json"
    TEST_IMAGE_ROOT = "../data/test/DCM"
    
    # Model
    TRAIN_BATCH_SIZE = 1
    VAL_BATCH_SIZE = 4
    LEARNING_RATE = 1e-4
    NUM_EPOCHS = 5
    VAL_EVERY = 1
    RANDOM_SEED = 21

    # Loss
    LOSS_TYPE = "bce"

    # Scheduler
    SCHEDULER_TYPE = "reduce"
    MIN_LR = 1e-6
    
    # Paths
    SAVED_DIR = Path("checkpoints")
    SAVED_DIR.mkdir(exist_ok=True)
    
    # Classes
    CLASSES = [
        'finger-1', 'finger-2', 'finger-3', 'finger-4', 'finger-5',
        'finger-6', 'finger-7', 'finger-8', 'finger-9', 'finger-10',
        'finger-11', 'finger-12', 'finger-13', 'finger-14', 'finger-15',
        'finger-16', 'finger-17', 'finger-18', 'finger-19', 'Trapezium',
        'Trapezoid', 'Capitate', 'Hamate', 'Scaphoid', 'Lunate',
        'Triquetrum', 'Pisiform', 'Radius', 'Ulna',
    ]
    
    CLASS2IND = {v: i for i, v in enumerate(CLASSES)}
    IND2CLASS = {v: k for k, v in CLASS2IND.items()}