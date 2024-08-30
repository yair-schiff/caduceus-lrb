python caduceus/finetuning_glrb/main.py \
    --task "variant_effect_causal_eqtl" \
    --seq_len 131072 \
    --model_name "kuleshov-group/caduceus-ps_seqlen-131k_d_model-256_n_layer-16" \
    --bp_per_token 1 \
    --save_dir "output/" \
    --wandb_api_key "${WANDB_API_KEY}" \
    --name_wb "test_caduceus_vep" \
    --train_batch_size 1 \
    --test_batch_size 2 \
    --rcps true \
    --num_workers 1 \
    --num_epochs 1 \
    --precision "16-mixed" \
    --learning_rate "1e-5" \
    --log_interval 4480 \
    --accumulate_grad_batches 64 \
    --train_ratio 1.0 \
    --eval_ratio 1.0 \
    --num_devices 1

##Examples

## Caduceus-PS
#task=bulk_rna_expression
#seq_len=131000
#bp_per_token=1
#model_name="kuleshov-group/caduceus-ps_seqlen-131k_d_model-256_n_layer-16"
#rcps=true

## NTv2
#task=regulatory_element_promoter
#seq_len=12288  # 2048 (seq len) * 6 (kmers)
#bp_per_token=6
#model_name="InstaDeepAI/nucleotide-transformer-v2-50m-multi-species"
#delete the rcps flag (it is not a RC-equivarient model)
