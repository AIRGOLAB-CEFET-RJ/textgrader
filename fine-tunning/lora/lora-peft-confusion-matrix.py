import json
from datetime import datetime
import time
import torch
from torch.optim import AdamW
from torch.utils.data import DataLoader
from peft import (
    get_peft_model,
    LoraConfig,
    PeftType,
)
import sys
import evaluate
from datasets import load_dataset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    get_linear_schedule_with_warmup,
)
from tqdm import tqdm
from sklearn.metrics import confusion_matrix, ConfusionMatrixDisplay
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

if len(sys.argv) < 2:
    print("Uso: python meu_script.py <conjunto> <obs-opcional>")
    sys.exit()

conjunto = sys.argv[1]
if len(sys.argv) > 2:
    obs = sys.argv[2]
else:
    obs = ""

start_time = time.time()

## Definindo configurações
batch_size = 5
model_name_or_path = "roberta-large"
task = "mrpc"
peft_type = PeftType.LORA
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
num_epochs = 5
lr = 3e-4
padding_side = "right"
n_labels = 33

peft_config = LoraConfig(
    task_type="SEQ_CLS",
    inference_mode=False,
    r=8,
    lora_alpha=16,
    lora_dropout=0.1,
    use_dora=True,
)

tokenizer = AutoTokenizer.from_pretrained(model_name_or_path, padding=padding_side)
if getattr(tokenizer, "pad_token_id") is None:
    tokenizer.pad_token_id = tokenizer.eos_token_id

# datasets = load_dataset("glue", task)
datasets = load_dataset(
    "parquet", data_files=f"preprocessing/train_conjunto_{conjunto}_output.parquet"
)
datasets_test = load_dataset(
    "parquet", data_files=f"preprocessing/test_conjunto_{conjunto}_output.parquet"
)
datasets_eval = load_dataset(
    "parquet", data_files=f"preprocessing/eval_conjunto_{conjunto}_output.parquet"
)

metric = evaluate.load("accuracy")


def tokenize(examples):
    outputs = tokenizer(examples["texto"], truncation=True, max_length=512)
    return outputs


tokenize_datasets = datasets.map(
    tokenize,
    batched=True,
    remove_columns=["texto", "nota"],
)


tokenize_datasets_test = datasets_test.map(
    tokenize,
    batched=True,
    remove_columns=["texto", "nota"],
)

tokenize_datasets_eval = datasets_eval.map(
    tokenize,
    batched=True,
    remove_columns=["texto", "nota"],
)

# tokenize_datasets = tokenize_datasets.rename_column("label", "labels")


def collate_fn(examples):
    return tokenizer.pad(examples, padding="longest", return_tensors="pt")


train_dataloader = DataLoader(
    tokenize_datasets["train"],
    shuffle=True,
    collate_fn=collate_fn,
    batch_size=batch_size,
)
test_dataloader = DataLoader(
    tokenize_datasets_test["train"],
    shuffle=False,
    collate_fn=collate_fn,
    batch_size=batch_size,
)
eval_dataloader = DataLoader(
    tokenize_datasets_eval["train"],
    shuffle=False,
    collate_fn=collate_fn,
    batch_size=batch_size,
)

model = AutoModelForSequenceClassification.from_pretrained(
    model_name_or_path, return_dict=True, num_labels=n_labels
)
model = get_peft_model(model, peft_config)
model.print_trainable_parameters()
print(model)

optimizer = AdamW(model.parameters(), lr=lr)

lr_scheduler = get_linear_schedule_with_warmup(
    optimizer=optimizer,
    num_warmup_steps=0.06 * (len(train_dataloader) * num_epochs),
    num_training_steps=(len(train_dataloader) * num_epochs),
)

model.to(device)
results = {
    "batch_size": batch_size,
    "model": model_name_or_path,
    "epochs": num_epochs,
    "metrics": {},
    "conjunto": conjunto,
    "obs": obs,
    "padding_side": padding_side,
    "train_size": len(tokenize_datasets["train"]["input_ids"]),
    "test_size": len(tokenize_datasets_test["train"]["input_ids"]),
    "eval_size": len(tokenize_datasets_eval["train"]["input_ids"]),
    "n_labels": n_labels,
}

for epoch in range(num_epochs):
    model.train()
    for step, batch in enumerate(train_dataloader):
        batch.to(device)
        outputs = model(**batch)
        loss = outputs.loss
        loss.backward()
        optimizer.step()
        lr_scheduler.step()
        optimizer.zero_grad()

    model.eval()
    for step, batch in enumerate(tqdm(test_dataloader)):
        batch.to(device)
        with torch.no_grad():
            outputs = model(**batch)
        predictions = outputs.logits.argmax(dim=-1)
        predictions, references = predictions, batch["labels"]
        print(f"predictions: {predictions} references: {references}")
        metric.add_batch(
            predictions=predictions,
            references=references,
        )

    test_metric = metric.compute()
    print(f"epoch {epoch}:", test_metric)
    results["metrics"][epoch] = test_metric

## using evaluation data
all_predictions = []
all_references = []

for step, batch in enumerate(tqdm(eval_dataloader)):
    batch.to(device)
    with torch.no_grad():
        outputs = model(**batch)
    predictions = outputs.logits.argmax(dim=-1)
    predictions, references = predictions, batch["labels"]
    all_predictions.extend(predictions.cpu().numpy())
    all_references.extend(references.cpu().numpy())
    print(f"predictions: {predictions} references: {references}")
    metric.add_batch(
        predictions=predictions,
        references=references,
    )

eval_metric = metric.compute()
print(f"Validation metric: {eval_metric}")
results["validation_metric"] = eval_metric

## Calcular a matriz de confusão
cm = confusion_matrix(all_references, all_predictions)

## Salvar a matriz de confusão como CSV
cm_df = pd.DataFrame(cm)
cm_df.to_csv(f"results/confusion_matrix_{conjunto}_{num_epochs}_epochs.csv", index=False)

## Visualizar a matriz de confusão
disp = ConfusionMatrixDisplay(confusion_matrix=cm)
disp.plot(cmap=plt.cm.Blues)
plt.title("Confusion Matrix")

## Salvar a matriz de confusão como PNG
plt.savefig(f"results/confusion_matrix_{conjunto}_{num_epochs}_epochs.png")
plt.show()

## Saving log file
today = datetime.now().strftime('%d-%m-%Y-%H-%M')
end_time = time.time()
elapsed_time = end_time - start_time
results["date"] = today
results["processing_time"] = elapsed_time/60

with open(
    f"results/{today}-conjunto{conjunto}-{num_epochs}-epochs.json",
    "w",
    encoding="utf-8",
) as arquivo:
    json.dump(results, arquivo, indent=4)

# model.save_pretrained()
print("finish!!")