import os
import sys
import time
import random
import argparse
import numpy as np
import pandas as pd
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import TensorDataset, DataLoader, random_split
from transformers import BertModel, BertConfig, AutoTokenizer
from sklearn.metrics import roc_auc_score, average_precision_score, f1_score, recall_score, precision_score
from sklearn.model_selection import train_test_split

DEBUG = True

# Loss Function and Utility Functions
def compute_class_weights(df, label_column):
    # INS method: weight = total_samples / (class_count * num_classes)
    class_counts = df[label_column].value_counts().sort_index()
    total_samples = len(df)
    num_classes = len(class_counts)
    class_weights = total_samples / (class_counts * num_classes)
    return class_weights

def get_pos_weight(labels_series, device, clip_max=10.0):
    # Compute weight for the positive class: negative_count/positive_count (clipped)
    positive = labels_series.sum()
    negative = len(labels_series) - positive
    if positive == 0:
        weight = torch.tensor(1.0, dtype=torch.float, device=device)
    else:
        w = negative / positive
        w = min(w, clip_max)
        weight = torch.tensor(w, dtype=torch.float, device=device)
    if DEBUG:
        print("Positive weight:", weight.item())
    return weight

# Sensitive Attribute Mapping Functions
def get_age_bucket(age):
    if 15 <= age <= 29:
        return "15-29"
    elif 30 <= age <= 49:
        return "30-49"
    elif 50 <= age <= 69:
        return "50-69"
    elif 70 <= age <= 89:
        return "70-89"
    else:
        return "Other"

def map_ethnicity(code):
    mapping = {0: "white", 1: "black", 2: "asian", 3: "hispanic"}
    return mapping.get(code, "other")

def map_insurance(code):
    mapping = {0: "government", 1: "medicare", 2: "medicaid", 3: "private", 4: "self pay"}
    return mapping.get(code, "other")

# EDDI Calculation Function
def compute_eddi(true_labels, predicted_labels, sensitive_labels, threshold=0.5):
    preds = (predicted_labels > threshold).astype(int)
    overall_error = np.mean(preds != true_labels)
    norm_factor = max(overall_error, 1 - overall_error)
    unique_groups = np.unique(sensitive_labels)
    subgroup_eddi = {}
    for group in unique_groups:
        mask = (sensitive_labels == group)
        if np.sum(mask) == 0:
            subgroup_eddi[group] = np.nan
        else:
            group_error = np.mean(preds[mask] != true_labels[mask])
            d_s = (group_error - overall_error) / norm_factor
            subgroup_eddi[group] = d_s
    eddi_attr = np.sqrt(np.sum(np.array(list(subgroup_eddi.values()))**2)) / len(unique_groups)
    return eddi_attr, subgroup_eddi

# BioClinicalBERT Fine-Tuning and Note Aggregation
class BioClinicalBERT_FT(nn.Module):
    def __init__(self, base_model, config, device):
        super(BioClinicalBERT_FT, self).__init__()
        self.BioBert = base_model
        self.device = device

    def forward(self, input_ids, attention_mask):
        outputs = self.BioBert(input_ids=input_ids, attention_mask=attention_mask)
        cls_embedding = outputs.last_hidden_state[:, 0, :]
        return cls_embedding

def apply_bioclinicalbert_on_patient_notes(df, note_columns, tokenizer, model, device, aggregation="mean"):
    patient_ids = df["subject_id"].unique()
    aggregated_embeddings = []
    for pid in tqdm(patient_ids, desc="Aggregating text embeddings"):
        patient_data = df[df["subject_id"] == pid]
        notes = []
        for col in note_columns:
            vals = patient_data[col].dropna().tolist()
            notes.extend([v for v in vals if isinstance(v, str) and v.strip() != ""])
        if len(notes) == 0:
            aggregated_embeddings.append(np.zeros(model.BioBert.config.hidden_size))
        else:
            embeddings = []
            for note in notes:
                encoded = tokenizer.encode_plus(
                    text=note,
                    add_special_tokens=True,
                    max_length=512,
                    padding='max_length',
                    truncation=True,
                    return_attention_mask=True,
                    return_tensors='pt'
                )
                input_ids = encoded['input_ids'].to(device)
                attn_mask = encoded['attention_mask'].to(device)
                with torch.no_grad():
                    emb = model(input_ids, attn_mask)
                embeddings.append(emb.cpu().numpy())
            embeddings = np.vstack(embeddings)
            agg_emb = np.mean(embeddings, axis=0) if aggregation=="mean" else np.max(embeddings, axis=0)
            aggregated_embeddings.append(agg_emb)
    aggregated_embeddings = np.vstack(aggregated_embeddings)
    return aggregated_embeddings

# BEHRT Model for Structured Data
class BEHRTModel(nn.Module):
    def __init__(self, num_diseases, num_ages, num_segments, num_admission_locs, num_discharge_locs, 
                 num_genders, num_ethnicities, num_insurances, hidden_size=768):
        super(BEHRTModel, self).__init__()
        vocab_size = num_diseases + num_ages + num_segments + num_admission_locs + num_discharge_locs + 2
        config = BertConfig(
            vocab_size=vocab_size,
            hidden_size=hidden_size,
            num_hidden_layers=12,
            num_attention_heads=12,
            intermediate_size=3072,
            max_position_embeddings=512,
            type_vocab_size=2,
            hidden_dropout_prob=0.1,
            attention_probs_dropout_prob=0.1
        )
        self.bert = BertModel(config)
        self.age_embedding = nn.Embedding(num_ages, hidden_size)
        self.segment_embedding = nn.Embedding(num_segments, hidden_size)
        self.admission_loc_embedding = nn.Embedding(num_admission_locs, hidden_size)
        self.discharge_loc_embedding = nn.Embedding(num_discharge_locs, hidden_size)
        self.gender_embedding = nn.Embedding(num_genders, hidden_size)
        self.ethnicity_embedding = nn.Embedding(num_ethnicities, hidden_size)
        self.insurance_embedding = nn.Embedding(num_insurances, hidden_size)

    def forward(self, input_ids, attention_mask, age_ids, segment_ids, adm_loc_ids, disch_loc_ids,
                gender_ids, ethnicity_ids, insurance_ids):
        # Clamp indices to avoid out-of-bound errors
        age_ids = torch.clamp(age_ids, 0, self.age_embedding.num_embeddings - 1)
        segment_ids = torch.clamp(segment_ids, 0, self.segment_embedding.num_embeddings - 1)
        adm_loc_ids = torch.clamp(adm_loc_ids, 0, self.admission_loc_embedding.num_embeddings - 1)
        disch_loc_ids = torch.clamp(disch_loc_ids, 0, self.discharge_loc_embedding.num_embeddings - 1)
        gender_ids = torch.clamp(gender_ids, 0, self.gender_embedding.num_embeddings - 1)
        ethnicity_ids = torch.clamp(ethnicity_ids, 0, self.ethnicity_embedding.num_embeddings - 1)
        insurance_ids = torch.clamp(insurance_ids, 0, self.insurance_embedding.num_embeddings - 1)

        outputs = self.bert(input_ids=input_ids, attention_mask=attention_mask)
        cls_token = outputs.last_hidden_state[:, 0, :]
        age_embeds = self.age_embedding(age_ids)
        segment_embeds = self.segment_embedding(segment_ids)
        adm_embeds = self.admission_loc_embedding(adm_loc_ids)
        disch_embeds = self.discharge_loc_embedding(disch_loc_ids)
        gender_embeds = self.gender_embedding(gender_ids)
        eth_embeds = self.ethnicity_embedding(ethnicity_ids)
        ins_embeds = self.insurance_embedding(insurance_ids)
        extra = (age_embeds + segment_embeds + adm_embeds + disch_embeds +
                 gender_embeds + eth_embeds + ins_embeds) / 7.0
        cls_embedding = cls_token + extra
        return cls_embedding

# Multimodal Transformer Model
class MultimodalTransformer(nn.Module):
    def __init__(self, text_embed_size, BEHRT, device, hidden_size=512):
        super(MultimodalTransformer, self).__init__()
        self.BEHRT = BEHRT
        self.device = device

        self.ts_projector = nn.Sequential(
            nn.Linear(BEHRT.bert.config.hidden_size, 256),
            nn.ReLU()
        )
        self.text_projector = nn.Sequential(
            nn.Linear(text_embed_size, 256),
            nn.ReLU()
        )
        # Classifier outputs three values: mortality, LOS, and mechanical ventilation
        self.classifier = nn.Sequential(
            nn.Linear(256 + 256, hidden_size),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(hidden_size, 3)
        )

    def forward(self, dummy_input_ids, dummy_attn_mask, 
                age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
                gender_ids, ethnicity_ids, insurance_ids,
                aggregated_text_embedding):
        structured_emb = self.BEHRT(dummy_input_ids, dummy_attn_mask,
                                    age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
                                    gender_ids, ethnicity_ids, insurance_ids)
        ts_proj = self.ts_projector(structured_emb)
        text_proj = self.text_projector(aggregated_text_embedding)
        combined = torch.cat((ts_proj, text_proj), dim=1)
        logits = self.classifier(combined)
        mortality_logits = logits[:, 0].unsqueeze(1)
        los_logits = logits[:, 1].unsqueeze(1)
        vent_logits = logits[:, 2].unsqueeze(1)
        return mortality_logits, los_logits, vent_logits

# Training and Evaluation Functions
def train_step(model, dataloader, optimizer, device, crit_mort, crit_los, crit_vent):
    model.train()
    running_loss = 0.0
    for batch in dataloader:
        (dummy_input_ids, dummy_attn_mask,
         age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
         gender_ids, ethnicity_ids, insurance_ids,
         aggregated_text_embedding,
         labels_mortality, labels_los, labels_vent) = [x.to(device) for x in batch]

        optimizer.zero_grad()
        mortality_logits, los_logits, vent_logits = model(
            dummy_input_ids, dummy_attn_mask,
            age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
            gender_ids, ethnicity_ids, insurance_ids,
            aggregated_text_embedding
        )
        loss_mort = crit_mort(mortality_logits, labels_mortality.unsqueeze(1))
        loss_los = crit_los(los_logits, labels_los.unsqueeze(1))
        loss_vent = crit_vent(vent_logits, labels_vent.unsqueeze(1))
        loss = loss_mort + loss_los + loss_vent
        loss.backward()
        optimizer.step()
        running_loss += loss.item()
    return running_loss

def evaluate_model(model, dataloader, device, threshold=0.5, print_eddi=False):
    model.eval()
    all_mort_logits = []
    all_los_logits = []
    all_mech_logits = []
    all_labels_mort = []
    all_labels_los = []
    all_labels_mech = []
    all_age = []
    all_ethnicity = []
    all_insurance = []
    
    with torch.no_grad():
        for batch in dataloader:
            (dummy_input_ids, dummy_attn_mask,
             age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
             gender_ids, ethnicity_ids, insurance_ids,
             aggregated_text_embedding,
             labels_mortality, labels_los, labels_vent) = [x.to(device) for x in batch]
            
            mort_logits, los_logits, mech_logits = model(
                dummy_input_ids, dummy_attn_mask,
                age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
                gender_ids, ethnicity_ids, insurance_ids,
                aggregated_text_embedding
            )
            all_mort_logits.append(mort_logits.cpu())
            all_los_logits.append(los_logits.cpu())
            all_mech_logits.append(mech_logits.cpu())
            all_labels_mort.append(labels_mortality.cpu())
            all_labels_los.append(labels_los.cpu())
            all_labels_mech.append(labels_vent.cpu())
            all_age.append(age_ids.cpu())
            all_ethnicity.append(ethnicity_ids.cpu())
            all_insurance.append(insurance_ids.cpu())
    
    all_mort_logits = torch.cat(all_mort_logits, dim=0)
    all_los_logits  = torch.cat(all_los_logits, dim=0)
    all_mech_logits = torch.cat(all_mech_logits, dim=0)
    all_labels_mort = torch.cat(all_labels_mort, dim=0)
    all_labels_los  = torch.cat(all_labels_los, dim=0)
    all_labels_mech = torch.cat(all_labels_mech, dim=0)
    
    mort_probs = torch.sigmoid(all_mort_logits).numpy().squeeze()
    los_probs  = torch.sigmoid(all_los_logits).numpy().squeeze()
    mech_probs = torch.sigmoid(all_mech_logits).numpy().squeeze()
    labels_mort_np = all_labels_mort.numpy().squeeze()
    labels_los_np  = all_labels_los.numpy().squeeze()
    labels_mech_np = all_labels_mech.numpy().squeeze()
    
    metrics = {}
    # For each outcome, calculate AUC, AUPRC, F1, TPR (recall), Precision, and FPR.
    for task, probs, labels in zip(["mortality", "los", "mechanical_ventilation"],
                                    [mort_probs, los_probs, mech_probs],
                                    [labels_mort_np, labels_los_np, labels_mech_np]):
        try:
            aucroc = roc_auc_score(labels, probs)
        except Exception:
            aucroc = float('nan')
        try:
            auprc = average_precision_score(labels, probs)
        except Exception:
            auprc = float('nan')
        preds = (probs > threshold).astype(int)
        f1 = f1_score(labels, preds, zero_division=0)
        tpr = recall_score(labels, preds, zero_division=0)
        precision = precision_score(labels, preds, zero_division=0)
        fp = np.sum((preds == 1) & (labels == 0))
        tn = np.sum((preds == 0) & (labels == 0))
        fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
        
        metrics[task] = {"aucroc": aucroc, "auprc": auprc, "f1": f1,
                         "tpr": tpr, "precision": precision, "fpr": fpr}
    
    # Get sensitive attribute groups.
    ages = torch.cat(all_age, dim=0).numpy().squeeze()
    ethnicities = torch.cat(all_ethnicity, dim=0).numpy().squeeze()
    insurances = torch.cat(all_insurance, dim=0).numpy().squeeze()
    
    age_groups = np.array([get_age_bucket(a) for a in ages])
    ethnicity_groups = np.array([map_ethnicity(e) for e in ethnicities])
    insurance_groups = np.array([map_insurance(i) for i in insurances])
    
    age_order = ["15-29", "30-49", "50-69", "70-89", "Other"]
    ethnicity_order = ["white", "black", "asian", "hispanic", "other"]
    insurance_order = ["government", "medicare", "medicaid", "private", "self pay", "other"]
    
    eddi_stats = {}
    for task, labels_np, probs in zip(["mortality", "los", "mechanical_ventilation"],
                                      [labels_mort_np, labels_los_np, labels_mech_np],
                                      [mort_probs, los_probs, mech_probs]):
        overall_age, age_eddi_sub = compute_eddi(labels_np.astype(int), probs, age_groups, threshold)
        overall_eth, eth_eddi_sub = compute_eddi(labels_np.astype(int), probs, ethnicity_groups, threshold)
        overall_ins, ins_eddi_sub = compute_eddi(labels_np.astype(int), probs, insurance_groups, threshold)
        total_eddi = np.sqrt((overall_age**2 + overall_eth**2 + overall_ins**2)) / 3
        eddi_stats[task] = {
            "age_eddi": overall_age,
            "age_subgroup_eddi": age_eddi_sub,
            "ethnicity_eddi": overall_eth,
            "ethnicity_subgroup_eddi": eth_eddi_sub,
            "insurance_eddi": overall_ins,
            "insurance_subgroup_eddi": ins_eddi_sub,
            "final_EDDI": total_eddi
        }
    
    metrics["eddi_stats"] = eddi_stats
    
    if print_eddi:
        print("\n--- EDDI Calculation for Each Outcome ---")
        for task in ["mortality", "los", "mechanical_ventilation"]:
            print(f"\nTask: {task.capitalize()}")
            eddi = eddi_stats[task]
            print("  Aggregated Age EDDI    : {:.4f}".format(eddi["age_eddi"]))
            print("  Age Subgroup EDDI:")
            for bucket in age_order:
                score = eddi["age_subgroup_eddi"].get(bucket, 0)
                print(f"    {bucket}: {score:.4f}")
            print("  Aggregated Ethnicity EDDI: {:.4f}".format(eddi["ethnicity_eddi"]))
            print("  Ethnicity Subgroup EDDI:")
            for group in ethnicity_order:
                score = eddi["ethnicity_subgroup_eddi"].get(group, 0)
                print(f"    {group}: {score:.4f}")
            print("  Aggregated Insurance EDDI: {:.4f}".format(eddi["insurance_eddi"]))
            print("  Insurance Subgroup EDDI:")
            for group in insurance_order:
                score = eddi["insurance_subgroup_eddi"].get(group, 0)
                print(f"    {group}: {score:.4f}")
            print("  Final Overall {} EDDI: {:.4f}".format(task.capitalize(), eddi["final_EDDI"]))
    
    return metrics

# Training Pipeline Function
def train_pipeline():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Using device:", device)

    keep_cols = {"subject_id", "hadm_id", "short_term_mortality", "los_binary", 
                 "mechanical_ventilation", "age", "first_wardid", "last_wardid", "ethnicity", "insurance", "gender"}
    
    structured_data = pd.read_csv('final_structured_common.csv')
    new_columns = {col: f"{col}_struct" for col in structured_data.columns if col not in keep_cols}
    structured_data.rename(columns=new_columns, inplace=True)

    unstructured_data = pd.read_csv("final_unstructured_common.csv", low_memory=False)
    unstructured_data.drop(
        columns=["short_term_mortality", "los_binary", "mechanical_ventilation", "age", "segment", 
                 "admission_loc", "discharge_loc", "gender", "ethnicity", "insurance"],
        errors='ignore',
        inplace=True
    )
    
    merged_df = pd.merge(
        structured_data,
        unstructured_data,
        on=["subject_id", "hadm_id"],
        how="inner"
    )
    
    if merged_df.empty:
        raise ValueError("Merged DataFrame is empty. Check your data and merge keys.")

    merged_df.columns = [col.lower().strip() for col in merged_df.columns]
    if "age_struct" in merged_df.columns:
        merged_df.rename(columns={"age_struct": "age"}, inplace=True)
    if "age" not in merged_df.columns:
        print("Column 'age' not found; creating default 'age' column with zeros.")
        merged_df["age"] = 0

    # Convert outcome columns to int.
    merged_df["short_term_mortality"] = merged_df["short_term_mortality"].astype(int)
    merged_df["los_binary"] = merged_df["los_binary"].astype(int)
    merged_df["mechanical_ventilation"] = merged_df["mechanical_ventilation"].astype(int)

    note_columns = [col for col in merged_df.columns if col.startswith("note_")]
    def has_valid_note(row):
        for col in note_columns:
            if pd.notnull(row[col]) and isinstance(row[col], str) and row[col].strip():
                return True
        return False
    df_filtered = merged_df[merged_df.apply(has_valid_note, axis=1)].copy()
    print("After filtering, number of rows:", len(df_filtered))

    required_cols = ["age", "first_wardid", "last_wardid", "gender", "ethnicity", "insurance"]
    for col in required_cols:
        if col not in df_filtered.columns:
            print(f"Column {col} not found; creating default values.")
            df_filtered[col] = 0

    df_unique = df_filtered.groupby("subject_id", as_index=False).first()
    print("Number of unique patients:", len(df_unique))
    
    if "segment" not in df_unique.columns:
        df_unique["segment"] = 0

    # Data Splitting (80% train-test; 5% of train as validation)
    train_val_df, test_df = train_test_split(df_unique, test_size=0.20, random_state=42)
    train_df, val_df = train_test_split(train_val_df, test_size=0.05, random_state=42)
    print(f"Train samples: {len(train_df)}, Validation samples: {len(val_df)}, Test samples: {len(test_df)}")
    
    # Initialize tokenizer and BioClinicalBERT.
    tokenizer = AutoTokenizer.from_pretrained("emilyalsentzer/Bio_ClinicalBERT")
    bioclinical_bert_base = BertModel.from_pretrained("emilyalsentzer/Bio_ClinicalBERT")
    bioclinical_bert_ft = BioClinicalBERT_FT(bioclinical_bert_base, bioclinical_bert_base.config, device).to(device)
    
    # For text aggregation, we use the train_df (or unique patients) – here we use the full df_unique.
    aggregated_text_embeddings_np = apply_bioclinicalbert_on_patient_notes(
        df_unique, note_columns, tokenizer, bioclinical_bert_ft, device, aggregation="mean"
    )
    aggregated_text_embeddings_t = torch.tensor(aggregated_text_embeddings_np, dtype=torch.float32)

    # Ensure demographic columns exist and convert to numeric codes if necessary.
    demographics_cols = ["age", "gender", "ethnicity", "insurance"]
    for col in demographics_cols:
        if col not in df_unique.columns:
            print(f"Column {col} not found; creating default values.")
            df_unique[col] = 0
        elif df_unique[col].dtype == object:
            df_unique[col] = df_unique[col].astype("category").cat.codes

    # Identify lab feature columns and fill missing values.
    exclude_cols = set(["subject_id", "row_id", "hadm_id", "icustay_id",
                        "short_term_mortality", "los_binary", "mechanical_ventilation",
                        "age", "first_wardid", "last_wardid", "ethnicity", "insurance", "gender"])
    lab_feature_columns = [col for col in df_unique.columns 
                           if col not in exclude_cols and not col.startswith("note_") 
                           and pd.api.types.is_numeric_dtype(df_unique[col])]
    print("Number of lab feature columns:", len(lab_feature_columns))
    df_unique[lab_feature_columns] = df_unique[lab_feature_columns].fillna(0)

    # Create tensors from df_unique (assumed to align with aggregated embeddings order).
    num_samples = len(df_unique)
    dummy_input_ids = torch.zeros((num_samples, 1), dtype=torch.long)
    dummy_attn_mask = torch.ones((num_samples, 1), dtype=torch.long)

    age_ids = torch.tensor(df_unique["age"].values, dtype=torch.long)
    segment_ids = torch.tensor(df_unique["segment"].values, dtype=torch.long)
    admission_loc_ids = torch.tensor(df_unique["first_wardid"].values, dtype=torch.long)
    discharge_loc_ids = torch.tensor(df_unique["last_wardid"].values, dtype=torch.long)
    gender_ids = torch.tensor(df_unique["gender"].values, dtype=torch.long)
    ethnicity_ids = torch.tensor(df_unique["ethnicity"].values, dtype=torch.long)
    insurance_ids = torch.tensor(df_unique["insurance"].values, dtype=torch.long)

    labels_mortality = torch.tensor(df_unique["short_term_mortality"].values, dtype=torch.float32)
    labels_los = torch.tensor(df_unique["los_binary"].values, dtype=torch.float32)
    labels_vent = torch.tensor(df_unique["mechanical_ventilation"].values, dtype=torch.float32)

    # Compute positive class weights (using training data only ideally)
    mortality_pos_weight = get_pos_weight(train_df["short_term_mortality"], device)
    los_pos_weight = get_pos_weight(train_df["los_binary"], device)
    mech_pos_weight = get_pos_weight(train_df["mechanical_ventilation"], device)
    
    # Use BCEWithLogitsLoss with computed pos_weight.
    criterion_mortality = nn.BCEWithLogitsLoss(pos_weight=mortality_pos_weight)
    criterion_los = nn.BCEWithLogitsLoss(pos_weight=los_pos_weight)
    criterion_mech = nn.BCEWithLogitsLoss(pos_weight=mech_pos_weight)
    
    # Build a dataset. (For simplicity, we assume the ordering of df_unique aligns with aggregated_text_embeddings_t.)
    dataset = TensorDataset(
        dummy_input_ids, dummy_attn_mask,
        age_ids, segment_ids, admission_loc_ids, discharge_loc_ids,
        gender_ids, ethnicity_ids, insurance_ids,
        aggregated_text_embeddings_t,
        labels_mortality, labels_los, labels_vent
    )
    
    # Split dataset into train, val, and test using the indices from our data splits.
    total_indices = np.arange(num_samples)
    train_indices = total_indices[df_unique["subject_id"].isin(train_df["subject_id"])]
    val_indices = total_indices[df_unique["subject_id"].isin(val_df["subject_id"])]
    test_indices = total_indices[df_unique["subject_id"].isin(test_df["subject_id"])]
    
    train_dataset = torch.utils.data.Subset(dataset, train_indices)
    val_dataset = torch.utils.data.Subset(dataset, val_indices)
    test_dataset = torch.utils.data.Subset(dataset, test_indices)
    
    train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=16, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=16, shuffle=False)

    # Define hyperparameters based on processed data.
    disease_mapping = {d: i for i, d in enumerate(df_unique["hadm_id"].unique())}
    NUM_DISEASES = len(disease_mapping)
    NUM_AGES = df_unique["age"].nunique()
    NUM_SEGMENTS = 2
    NUM_ADMISSION_LOCS = df_unique["first_wardid"].nunique()
    NUM_DISCHARGE_LOCS = df_unique["last_wardid"].nunique()
    NUM_GENDERS = df_unique["gender"].nunique()
    NUM_ETHNICITIES = df_unique["ethnicity"].nunique()
    NUM_INSURANCES = df_unique["insurance"].nunique()

    print("\n--- Hyperparameters based on processed data ---")
    print("NUM_DISEASES:", NUM_DISEASES)
    print("NUM_AGES:", NUM_AGES)
    print("NUM_SEGMENTS:", NUM_SEGMENTS)
    print("NUM_ADMISSION_LOCS:", NUM_ADMISSION_LOCS)
    print("NUM_DISCHARGE_LOCS:", NUM_DISCHARGE_LOCS)
    print("NUM_GENDERS:", NUM_GENDERS)
    print("NUM_ETHNICITIES:", NUM_ETHNICITIES)
    print("NUM_INSURANCES:", NUM_INSURANCES)

    behrt_model = BEHRTModel(
        num_diseases=NUM_DISEASES,
        num_ages=NUM_AGES,
        num_segments=NUM_SEGMENTS,
        num_admission_locs=NUM_ADMISSION_LOCS,
        num_discharge_locs=NUM_DISCHARGE_LOCS,
        num_genders=NUM_GENDERS,
        num_ethnicities=NUM_ETHNICITIES,
        num_insurances=NUM_INSURANCES,
        hidden_size=768
    ).to(device)

    multimodal_model = MultimodalTransformer(
        text_embed_size=768,
        BEHRT=behrt_model,
        device=device,
        hidden_size=512
    ).to(device)

    # Use AdamW with weight decay for regularization.
    optimizer = AdamW(multimodal_model.parameters(), lr=1e-4, weight_decay=1e-2)
    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=2, verbose=True)

    # Training loop with early stopping (patience = 5 epochs).
    num_epochs = 20
    best_val_loss = float('inf')
    patience = 5
    epochs_no_improve = 0
    best_model_state = None

    for epoch in range(num_epochs):
        multimodal_model.train()
        running_loss = train_step(multimodal_model, train_loader, optimizer, device,
                                  criterion_mortality, criterion_los, criterion_mech)
        train_loss = running_loss / len(train_loader)
        
        # Evaluate on validation set.
        multimodal_model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for batch in val_loader:
                (dummy_input_ids, dummy_attn_mask,
                 age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
                 gender_ids, ethnicity_ids, insurance_ids,
                 aggregated_text_embedding,
                 labels_mortality, labels_los, labels_vent) = [x.to(device) for x in batch]
                
                mortality_logits, los_logits, vent_logits = multimodal_model(
                    dummy_input_ids, dummy_attn_mask,
                    age_ids, segment_ids, adm_loc_ids, discharge_loc_ids,
                    gender_ids, ethnicity_ids, insurance_ids,
                    aggregated_text_embedding
                )
                loss_mort = criterion_mortality(mortality_logits, labels_mortality.unsqueeze(1))
                loss_los = criterion_los(los_logits, labels_los.unsqueeze(1))
                loss_vent = criterion_mech(vent_logits, labels_vent.unsqueeze(1))
                loss = loss_mort + loss_los + loss_vent
                val_loss += loss.item()
        val_loss /= len(val_loader)
        print(f"[Epoch {epoch+1}] Train Loss: {train_loss:.4f}, Val Loss: {val_loss:.4f}")
        scheduler.step(val_loss)
        
        # Early stopping check.
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_model_state = multimodal_model.state_dict()
            epochs_no_improve = 0
            print("Validation loss improved; saving model.")
        else:
            epochs_no_improve += 1
            print(f"No improvement in validation loss for {epochs_no_improve} epoch(s).")
            if epochs_no_improve >= patience:
                print("Early stopping triggered.")
                break

    # Load best model before final evaluation.
    if best_model_state is not None:
        multimodal_model.load_state_dict(best_model_state)

    # Evaluate on test set.
    metrics = evaluate_model(multimodal_model, test_loader, device, threshold=0.5, print_eddi=True)
    print("\nFinal Evaluation Metrics (Test Set):")
    for outcome in ["mortality", "los", "mechanical_ventilation"]:
        m = metrics[outcome]
        print(f"{outcome.capitalize()} - AUC-ROC: {m['aucroc']:.4f}, AUPRC: {m['auprc']:.4f}, "
              f"F1: {m['f1']:.4f}, TPR: {m['tpr']:.4f}, Precision: {m['precision']:.4f}, FPR: {m['fpr']:.4f}")
    
    print("\nDetailed EDDI Statistics:")
    eddi_stats = metrics["eddi_stats"]
    for outcome in ["mortality", "los", "mechanical_ventilation"]:
        print(f"\n{outcome.capitalize()} EDDI Stats:")
        stats = eddi_stats[outcome]
        print("  Age Subgroup EDDI      :", stats["age_subgroup_eddi"])
        print("  Aggregated Age EDDI    : {:.4f}".format(stats["age_eddi"]))
        print("  Ethnicity Subgroup EDDI:", stats["ethnicity_subgroup_eddi"])
        print("  Aggregated Ethnicity EDDI: {:.4f}".format(stats["ethnicity_eddi"]))
        print("  Insurance Subgroup EDDI:", stats["insurance_subgroup_eddi"])
        print("  Aggregated Insurance EDDI: {:.4f}".format(stats["insurance_eddi"]))
        print("  Final Overall {} EDDI: {:.4f}".format(outcome.capitalize(), stats["final_EDDI"]))

    print("Training complete.")

if __name__ == "__main__":
    train_pipeline()
