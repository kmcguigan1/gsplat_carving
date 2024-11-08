SCENE_DIR="data/360_v2"
RESULT_DIR="results/benchmark_4gpus"
# SCENE_LIST="garden bicycle stump bonsai counter kitchen room" # treehill flowers
SCENE_LIST="bonsai" # treehill flowers

for SCENE in $SCENE_LIST;
do
    if [ "$SCENE" = "bonsai" ] || [ "$SCENE" = "counter" ] || [ "$SCENE" = "kitchen" ] || [ "$SCENE" = "room" ]; then
        DATA_FACTOR=2
    else
        DATA_FACTOR=4
    fi

    echo "Running $SCENE"

# 3 GPUs are effectively 3x batch size, so we scale down the steps by 3x as well.
# "--packed" reduces the data transfer between GPUs, which leads to faster training.

CUDA_VISIBLE_DEVICES=0,1,2 python simple_trainer.py default --max_steps 300 --eval_steps 300 --disable_viewer --data_factor "$DATA_FACTOR" \
    --steps_scaler 0.25 --packed \
    --data_dir "data/360_v2/$SCENE/" \
    --result_dir "$RESULT_DIR/$SCENE/"


done


for SCENE in $SCENE_LIST;
do
    echo "=== Eval Stats ==="

    for STATS in $RESULT_DIR/$SCENE/stats/val_step7499.json;
    do  
        echo $STATS
        cat $STATS; 
        echo
    done

    echo "=== Train Stats ==="

    for STATS in $RESULT_DIR/$SCENE/stats/train_step7499_rank0.json;
    do  
        echo $STATS
        cat $STATS; 
        echo
    done
done