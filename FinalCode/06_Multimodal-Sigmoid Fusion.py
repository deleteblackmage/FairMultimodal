import os
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
from torch.utils.data import TensorDataset, DataLoader

from transformers import BertModel, BertConfig, AutoTokenizer
from sklearn.metrics import roc_auc_score, average_precision_score, f1_score, recall_score, precision_score

DEBUG = True

# Loss Functions and Class Weight Computation
class FocalLoss(nn.Module):
    def __init__(self, gamma=2, alpha=None, reduction='mean', pos_weight=None):
       
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.alpha = alpha
        self.reduction = reduction
        self.pos_weight = pos_weight

    def forward(self, logits, targets):
        bce_loss = F.binary_cross_entropy_with_logits(
            logits, targets, reduction='none', pos_weight=self.pos_weight
        )
        pt = torch.exp(-bce_loss)
        focal_loss = ((1 - pt) ** self.gamma) * bce_loss
        if self.alpha is not None:
            focal_loss = self.alpha * focal_loss
        if self.reduction == 'mean':
            return focal_loss.mean()
        elif self.reduction == 'sum':
            return focal_loss.sum()
        else:
            return focal_loss

def compute_class_weights(df, label_column):
    class_counts = df[label_column].value_counts().sort_index()
    total_samples = len(df)
    class_weights = total_samples / (class_counts * len(class_counts))
    return class_weights

def get_pos_weight(labels_series, device, clip_max=10.0):
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

# BioClinicalBERT Fine-Tuning and Note Aggregation
class BioClinicalBERT_FT(nn.Module):

    def __init__(self, base_model, config, device):
        super(BioClinicalBERT_FT, self).__init__()
        self.BioBert = base_model
        self.device = device

    def forward(self, input_ids, attention_mask):
        outputs = self.BioBert(input_ids=input_ids, attention_mask=attention_mask)
        cls_embedding = outputs.last_hidden_state[:, 0, :]  # CLS token
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
                    max_length=128,
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
            agg_emb = np.mean(embeddings, axis=0) if aggregation == "mean" else np.max(embeddings, axis=0)
            aggregated_embeddings.append(agg_emb)
    aggregated_embeddings = np.vstack(aggregated_embeddings)
    return aggregated_embeddings

# BEHRT Models for Structured Data

# Demographics Branch
class BEHRTModel_Demo(nn.Module):
    def __init__(self, num_ages, num_genders, num_ethnicities, num_insurances, hidden_size=768):
        super(BEHRTModel_Demo, self).__init__()
        vocab_size = num_ages + num_genders + num_ethnicities + num_insurances + 2
        config = BertConfig(
            vocab_size=vocab_size,
            hidden_size=hidden_size,
            num_hidden_layers=6,
            num_attention_heads=6,
            intermediate_size=3072,
            max_position_embeddings=128,
            type_vocab_size=2,
            hidden_dropout_prob=0.1,
            attention_probs_dropout_prob=0.1
        )
        self.bert = BertModel(config)
        self.age_embedding = nn.Embedding(num_ages, hidden_size)
        self.gender_embedding = nn.Embedding(num_genders, hidden_size)
        self.ethnicity_embedding = nn.Embedding(num_ethnicities, hidden_size)
        self.insurance_embedding = nn.Embedding(num_insurances, hidden_size)

    def forward(self, input_ids, attention_mask, age_ids, gender_ids, ethnicity_ids, insurance_ids):
        age_ids = torch.clamp(age_ids, 0, self.age_embedding.num_embeddings - 1)
        gender_ids = torch.clamp(gender_ids, 0, self.gender_embedding.num_embeddings - 1)
        ethnicity_ids = torch.clamp(ethnicity_ids, 0, self.ethnicity_embedding.num_embeddings - 1)
        insurance_ids = torch.clamp(insurance_ids, 0, self.insurance_embedding.num_embeddings - 1)
        outputs = self.bert(input_ids=input_ids, attention_mask=attention_mask)
        cls_token = outputs.last_hidden_state[:, 0, :]
        
        age_embeds = self.age_embedding(age_ids)
        gender_embeds = self.gender_embedding(gender_ids)
        eth_embeds = self.ethnicity_embedding(ethnicity_ids)
        ins_embeds = self.insurance_embedding(insurance_ids)
        extra = (age_embeds + gender_embeds + eth_embeds + ins_embeds) / 4.0
        demo_embedding = cls_token + extra
        return demo_embedding

# Lab Features Branch
class BEHRTModel_Lab(nn.Module):
    def __init__(self, lab_token_count, hidden_size=768, nhead=8, num_layers=2):

        super(BEHRTModel_Lab, self).__init__()
        self.hidden_size = hidden_size
        self.token_embedding = nn.Linear(1, hidden_size)
        self.pos_embedding = nn.Parameter(torch.randn(lab_token_count, hidden_size))
        encoder_layer = nn.TransformerEncoderLayer(d_model=hidden_size, nhead=nhead)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

    def forward(self, lab_features):
        x = lab_features.unsqueeze(-1)
        x = self.token_embedding(x)
        x = x + self.pos_embedding.unsqueeze(0)
        x = x.permute(1, 0, 2)
        x = self.transformer_encoder(x)
        x = x.permute(1, 0, 2)
        lab_embedding = x.mean(dim=1)
        return lab_embedding

# Multimodal Transformer: New Scalar per Branch Version with Sigmoid Fusion
class MultimodalTransformer(nn.Module):
    def __init__(self, text_embed_size, behrt_demo, behrt_lab, device, hidden_size=512):
        """
        Combines three branches:
          - Demographics (via BEHRTModel_Demo)
          - Lab features (via BEHRTModel_Lab)
          - Text (aggregated BioClinicalBERT embedding)
        Each branch is projected to 256 dimensions using a linear layer followed by ReLU.
        Then, each 256-dimensional projection is multiplied elementwise by its learnable weight vector
        (after applying sigmoid). The dot product (sum over the 256 dimensions) produces one scalar per branch.
        These three scalars are concatenated to form a 3-dimensional aggregated vector that is passed to a classifier.
        
        The final aggregated embedding (a 3-d vector, one scalar per branch) is also returned for the new EDDI calculation.
        """
        super(MultimodalTransformer, self).__init__()
        self.behrt_demo = behrt_demo
        self.behrt_lab = behrt_lab
        self.device = device

        self.demo_projector = nn.Sequential(
            nn.Linear(behrt_demo.bert.config.hidden_size, 256),
            nn.ReLU()
        )
        self.lab_projector = nn.Sequential(
            nn.Linear(behrt_lab.hidden_size, 256),
            nn.ReLU()
        )
        self.text_projector = nn.Sequential(
            nn.Linear(text_embed_size, 256),
            nn.ReLU()
        )

        # Classifier now takes in a 3-dimensional aggregated vector.
        self.classifier = nn.Sequential(
            nn.Linear(3, hidden_size),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(hidden_size, 2)
        )
        # Learnable parameters for adjusting each branch.
        self.sig_weights_demo = nn.Parameter(torch.randn(256))
        self.sig_weights_text = nn.Parameter(torch.randn(256))
        self.sig_weights_lab = nn.Parameter(torch.randn(256))

    def forward(self, demo_dummy_ids, demo_attn_mask,
                age_ids, gender_ids, ethnicity_ids, insurance_ids,
                lab_features, aggregated_text_embedding):
        # Demographics branch
        demo_embedding = self.behrt_demo(demo_dummy_ids, demo_attn_mask,
                                         age_ids, gender_ids, ethnicity_ids, insurance_ids)
        # Lab branch
        lab_embedding = self.behrt_lab(lab_features)
        # Text branch
        text_embedding = aggregated_text_embedding

        # Projection for each branch (output shape: batch x 256)
        demo_proj = self.demo_projector(demo_embedding)
        lab_proj = self.lab_projector(lab_embedding)
        text_proj = self.text_projector(text_embedding)
        
        # Apply learnable sigmoid weights (elementwise) to each branch.
        sig_weights_demo = torch.sigmoid(self.sig_weights_demo)
        sig_weights_lab = torch.sigmoid(self.sig_weights_lab)
        sig_weights_text = torch.sigmoid(self.sig_weights_text)
        
        demo_sigmoid = demo_proj * sig_weights_demo  # still batch x 256
        lab_sigmoid = lab_proj * sig_weights_lab
        text_sigmoid = text_proj * sig_weights_text
        
        # Dot products: reduce each branch's 256-d vector to one scalar per sample.
        demo_dot = torch.sum(demo_sigmoid, dim=1, keepdim=True)  
        lab_dot = torch.sum(lab_sigmoid, dim=1, keepdim=True)     
        text_dot = torch.sum(text_sigmoid, dim=1, keepdim=True)   
        
        # Concatenate the three scalars to form the final aggregated embedding (batch x 3).
        aggregated = torch.cat((demo_dot, lab_dot, text_dot), dim=1)
        
        logits = self.classifier(aggregated)
        mortality_logits = logits[:, 0].unsqueeze(1)
        readmission_logits = logits[:, 1].unsqueeze(1)
        
        # Return the final aggregated embedding for new EDDI calculation.
        return mortality_logits, readmission_logits, aggregated

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
        return "other"

def map_ethnicity(code):
    mapping = {0: "white", 1: "black", 2: "asian", 3: "hispanic"}
    return mapping.get(code, "others")

def map_insurance(code):
    mapping = {
        0: "government",
        1: "medicare",
        2: "medicaid",
        3: "private",
        4: "self pay"
    }
    return mapping.get(code, "others")

def compute_eddi(y_true, y_pred, sensitive_labels):
    """
    Computes the EDDI for a given sensitive attribute for one outcome.
    
    For each subgroup s:
      ER_s = mean(y_pred != y_true) for that subgroup
    OER = overall error rate over all samples.
    Then for each subgroup:
      EDDI_s = (ER_s - OER) / max(OER, 1-OER)
    And attribute-level EDDI is computed as:
      EDDI_attr = sqrt( (sum_s (EDDI_s^2)) / (number of subgroups) )
    
    Returns:
      eddi_attr: the computed attribute-level EDDI.
      subgroup_eddi: dictionary mapping subgroup to its EDDI value.
    """
    unique_groups = np.unique(sensitive_labels)
    subgroup_eddi = {}
    overall_error = np.mean(y_pred != y_true)
    denom = max(overall_error, 1 - overall_error) if overall_error not in [0, 1] else 1.0
    for group in unique_groups:
        mask = (sensitive_labels == group)
        if np.sum(mask) == 0:
            subgroup_eddi[group] = np.nan
        else:
            er_group = np.mean(y_pred[mask] != y_true[mask])
            subgroup_eddi[group] = (er_group - overall_error) / denom
    eddi_attr = np.sqrt(np.sum(np.array(list(subgroup_eddi.values()))**2)) / len(unique_groups)
    return eddi_attr, subgroup_eddi

def train_step(model, dataloader, optimizer, device, criterion_mortality, criterion_readmission):
    model.train()
    running_loss = 0.0
    for batch in dataloader:
        (demo_dummy_ids, demo_attn_mask,
         age_ids, gender_ids, ethnicity_ids, insurance_ids,
         lab_features,
         aggregated_text_embedding,
         labels_mortality, labels_readmission) = [x.to(device) for x in batch]
        optimizer.zero_grad()
        mortality_logits, readmission_logits, _ = model(
            demo_dummy_ids, demo_attn_mask,
            age_ids, gender_ids, ethnicity_ids, insurance_ids,
            lab_features, aggregated_text_embedding
        )
        loss_mortality = criterion_mortality(mortality_logits, labels_mortality.unsqueeze(1))
        loss_readmission = criterion_readmission(readmission_logits, labels_readmission.unsqueeze(1))
        loss = loss_mortality + loss_readmission
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        running_loss += loss.item()
    return running_loss

def evaluate_model(model, dataloader, device, threshold=0.5, print_eddi=False):
    model.eval()
    all_mort_logits = []
    all_readm_logits = []
    all_labels_mort = []
    all_labels_readm = []
    all_final_embeddings = []  # final aggregated embedding (3-d vector) from all three branches
    all_age = []
    all_ethnicity = []
    all_insurance = []
    
    with torch.no_grad():
        for batch in dataloader:
            (demo_dummy_ids, demo_attn_mask,
             age_ids, gender_ids, ethnicity_ids, insurance_ids,
             lab_features,
             aggregated_text_embedding,
             labels_mortality, labels_readmission) = [x.to(device) for x in batch]
            mort_logits, readm_logits, final_embedding = model(
                demo_dummy_ids, demo_attn_mask,
                age_ids, gender_ids, ethnicity_ids, insurance_ids,
                lab_features, aggregated_text_embedding
            )
            all_mort_logits.append(mort_logits.cpu())
            all_readm_logits.append(readm_logits.cpu())
            all_labels_mort.append(labels_mortality.cpu())
            all_labels_readm.append(labels_readmission.cpu())
            all_final_embeddings.append(final_embedding.cpu())
            all_age.append(age_ids.cpu())
            all_ethnicity.append(ethnicity_ids.cpu())
            all_insurance.append(insurance_ids.cpu())
    
    all_mort_logits = torch.cat(all_mort_logits, dim=0)
    all_readm_logits = torch.cat(all_readm_logits, dim=0)
    all_labels_mort = torch.cat(all_labels_mort, dim=0)
    all_labels_readm = torch.cat(all_labels_readm, dim=0)
    final_embeddings = torch.cat(all_final_embeddings, dim=0).numpy() 
    ages = torch.cat(all_age, dim=0).numpy()
    ethnicities = torch.cat(all_ethnicity, dim=0).numpy()
    insurances = torch.cat(all_insurance, dim=0).numpy()
    
    mort_probs = torch.sigmoid(all_mort_logits).numpy().squeeze()
    readm_probs = torch.sigmoid(all_readm_logits).numpy().squeeze()
    labels_mort_np = all_labels_mort.numpy().squeeze()
    labels_readm_np = all_labels_readm.numpy().squeeze()
    
    metrics = {}
    for task, probs, labels in zip(["mortality", "readmission"],
                                   [mort_probs, readm_probs],
                                   [labels_mort_np, labels_readm_np]):
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
        recall = recall_score(labels, preds, zero_division=0)
        precision = precision_score(labels, preds, zero_division=0)
        metrics[task] = {"aucroc": aucroc, "auprc": auprc, "f1": f1,
                         "recall": recall, "precision": precision}
    
    # EDDI Calculation for Each Outcome using the error-rate based formula 
    y_pred_mort = (mort_probs > threshold).astype(int)
    y_true_mort = labels_mort_np.astype(int)
    y_pred_readm = (readm_probs > threshold).astype(int)
    y_true_readm = labels_readm_np.astype(int)
    
    # Map sensitive attributes.
    age_groups = np.array([get_age_bucket(a) for a in ages])
    ethnicity_groups = np.array([map_ethnicity(e) for e in ethnicities])
    insurance_groups = np.array([map_insurance(i) for i in insurances])
    
    # Compute EDDI for mortality outcome.
    eddi_age_mort, age_eddi_sub_mort = compute_eddi(y_true_mort, y_pred_mort, age_groups)
    eddi_eth_mort, eth_eddi_sub_mort = compute_eddi(y_true_mort, y_pred_mort, ethnicity_groups)
    eddi_ins_mort, ins_eddi_sub_mort = compute_eddi(y_true_mort, y_pred_mort, insurance_groups)
    total_eddi_mort = np.sqrt((eddi_age_mort**2 + eddi_eth_mort**2 + eddi_ins_mort**2)) / 3
    
    # Compute EDDI for readmission outcome.
    eddi_age_readm, age_eddi_sub_readm = compute_eddi(y_true_readm, y_pred_readm, age_groups)
    eddi_eth_readm, eth_eddi_sub_readm = compute_eddi(y_true_readm, y_pred_readm, ethnicity_groups)
    eddi_ins_readm, ins_eddi_sub_readm = compute_eddi(y_true_readm, y_pred_readm, insurance_groups)
    total_eddi_readm = np.sqrt((eddi_age_readm**2 + eddi_eth_readm**2 + eddi_ins_readm**2)) / 3
    
    if print_eddi:
        print("\n--- EDDI Calculation for Mortality Outcome ---")
        print("Age Buckets EDDI (mortality):")
        for bucket, score in age_eddi_sub_mort.items():
            print(f"  {bucket}: {score:.4f}")
        print("Overall Age EDDI (mortality):", eddi_age_mort)
        
        print("\nEthnicity Groups EDDI (mortality):")
        for group, score in eth_eddi_sub_mort.items():
            print(f"  {group}: {score:.4f}")
        print("Overall Ethnicity EDDI (mortality):", eddi_eth_mort)
        
        print("\nInsurance Groups EDDI (mortality):")
        for group, score in ins_eddi_sub_mort.items():
            print(f"  {group}: {score:.4f}")
        print("Overall Insurance EDDI (mortality):", eddi_ins_mort)
        
        print("\nTotal EDDI for Mortality:", total_eddi_mort)
        
        print("\n--- EDDI Calculation for Readmission Outcome ---")
        print("Age Buckets EDDI (readmission):")
        for bucket, score in age_eddi_sub_readm.items():
            print(f"  {bucket}: {score:.4f}")
        print("Overall Age EDDI (readmission):", eddi_age_readm)
        
        print("\nEthnicity Groups EDDI (readmission):")
        for group, score in eth_eddi_sub_readm.items():
            print(f"  {group}: {score:.4f}")
        print("Overall Ethnicity EDDI (readmission):", eddi_eth_readm)
        
        print("\nInsurance Groups EDDI (readmission):")
        for group, score in ins_eddi_sub_readm.items():
            print(f"  {group}: {score:.4f}")
        print("Overall Insurance EDDI (readmission):", eddi_ins_readm)
        
        print("\nTotal EDDI for Readmission:", total_eddi_readm)
    
    # Build a detailed eddi stats dictionary.
    eddi_stats = {
        "mortality": {
            "age_subgroup_eddi": age_eddi_sub_mort,
            "age_eddi": eddi_age_mort,
            "ethnicity_subgroup_eddi": eth_eddi_sub_mort,
            "ethnicity_eddi": eddi_eth_mort,
            "insurance_subgroup_eddi": ins_eddi_sub_mort,
            "insurance_eddi": eddi_ins_mort,
            "final_EDDI": total_eddi_mort
        },
        "readmission": {
            "age_subgroup_eddi": age_eddi_sub_readm,
            "age_eddi": eddi_age_readm,
            "ethnicity_subgroup_eddi": eth_eddi_sub_readm,
            "ethnicity_eddi": eddi_eth_readm,
            "insurance_subgroup_eddi": ins_eddi_sub_readm,
            "insurance_eddi": eddi_ins_readm,
            "final_EDDI": total_eddi_readm
        }
    }
    
    metrics["eddi_stats"] = eddi_stats
    return metrics

def train_pipeline():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Using device:", device)

    # Merge Structured and Unstructured Data 
    structured_data = pd.read_csv('final_structured_common.csv')
    unstructured_data = pd.read_csv("unstructured_with_demographics.csv", low_memory=False)
    print("\n--- Debug Info: Before Merge ---")
    print("Structured data shape:", structured_data.shape)
    print("Unstructured data shape:", unstructured_data.shape)
    
    unstructured_data.drop(
        columns=["short_term_mortality", "readmission_within_30_days", "age",
                 "GENDER", "ETHNICITY", "INSURANCE"],
        errors='ignore',
        inplace=True
    )
    merged_df = pd.merge(
        structured_data,
        unstructured_data,
        on=["subject_id", "hadm_id"],
        how="inner",
        suffixes=("_struct", "_unstruct")
    )
    if merged_df.empty:
        raise ValueError("Merged DataFrame is empty. Check your merge keys.")
    
    merged_df["short_term_mortality"] = merged_df["short_term_mortality"].astype(int)
    merged_df["readmission_within_30_days"] = merged_df["readmission_within_30_days"].astype(int)
    
    note_columns = [col for col in merged_df.columns if col.startswith("note_")]
    def has_valid_note(row):
        for col in note_columns:
            if pd.notnull(row[col]) and isinstance(row[col], str) and row[col].strip():
                return True
        return False
    df_filtered = merged_df[merged_df.apply(has_valid_note, axis=1)].copy()
    print("After filtering, number of rows:", len(df_filtered))

    # Compute Aggregated Text Embeddings (Text Branch)
    print("Computing aggregated text embeddings for each patient...")
    tokenizer = AutoTokenizer.from_pretrained("emilyalsentzer/Bio_ClinicalBERT")
    bioclinical_bert_base = BertModel.from_pretrained("emilyalsentzer/Bio_ClinicalBERT")
    bioclinical_bert_ft = BioClinicalBERT_FT(bioclinical_bert_base, bioclinical_bert_base.config, device).to(device)
    aggregated_text_embeddings_np = apply_bioclinicalbert_on_patient_notes(
        df_filtered, note_columns, tokenizer, bioclinical_bert_ft, device, aggregation="mean"
    )
    print("Aggregated text embeddings shape:", aggregated_text_embeddings_np.shape)
    aggregated_text_embeddings_t = torch.tensor(aggregated_text_embeddings_np, dtype=torch.float32)

    # Process Structured Data: Split into Demographics and Lab Features 
    demographics_cols = ["age", "GENDER", "ETHNICITY", "INSURANCE"]
    for col in demographics_cols:
        if col not in df_filtered.columns:
            print(f"Column {col} not found; creating default values.")
            df_filtered[col] = 0
        elif df_filtered[col].dtype == object:
            df_filtered[col] = df_filtered[col].astype("category").cat.codes

    exclude_cols = set(["subject_id", "ROW_ID", "hadm_id", "ICUSTAY_ID", "DBSOURCE", "FIRST_CAREUNIT",
                        "LAST_CAREUNIT", "FIRST_WARDID", "LAST_WARDID", "INTIME", "OUTTIME", "LOS",
                        "ADMITTIME", "DISCHTIME", "DEATHTIME", "GENDER", "ETHNICITY", "INSURANCE",
                        "DOB", "short_term_mortality", "current_admission_dischtime", "next_admission_icu_intime",
                        "readmission_within_30_days", "age"])
    lab_feature_columns = [col for col in df_filtered.columns 
                           if col not in exclude_cols and not col.startswith("note_") 
                           and pd.api.types.is_numeric_dtype(df_filtered[col])]
    print("Number of lab feature columns:", len(lab_feature_columns))
    df_filtered[lab_feature_columns] = df_filtered[lab_feature_columns].fillna(0)

    # Normalize Lab Features 
    lab_features_np = df_filtered[lab_feature_columns].values.astype(np.float32)
    lab_mean = np.mean(lab_features_np, axis=0)
    lab_std = np.std(lab_features_np, axis=0)
    lab_features_np = (lab_features_np - lab_mean) / (lab_std + 1e-6)
    
    # Create Inputs for Each Branch
    num_samples = len(df_filtered)
    demo_dummy_ids = torch.zeros((num_samples, 1), dtype=torch.long)
    demo_attn_mask = torch.ones((num_samples, 1), dtype=torch.long)
    age_ids = torch.tensor(df_filtered["age"].values, dtype=torch.long)
    gender_ids = torch.tensor(df_filtered["GENDER"].values, dtype=torch.long)
    ethnicity_ids = torch.tensor(df_filtered["ETHNICITY"].values, dtype=torch.long)
    insurance_ids = torch.tensor(df_filtered["INSURANCE"].values, dtype=torch.long)
    lab_features_t = torch.tensor(lab_features_np, dtype=torch.float32)
    labels_mortality = torch.tensor(df_filtered["short_term_mortality"].values, dtype=torch.float32)
    labels_readmission = torch.tensor(df_filtered["readmission_within_30_days"].values, dtype=torch.float32)

    dataset = TensorDataset(
        demo_dummy_ids, demo_attn_mask,
        age_ids, gender_ids, ethnicity_ids, insurance_ids,
        lab_features_t,
        aggregated_text_embeddings_t,
        labels_mortality, labels_readmission
    )
    dataloader = DataLoader(dataset, batch_size=16, shuffle=True)

    NUM_AGES = df_filtered["age"].nunique()
    NUM_GENDERS = df_filtered["GENDER"].nunique()
    NUM_ETHNICITIES = df_filtered["ETHNICITY"].nunique()
    NUM_INSURANCES = df_filtered["INSURANCE"].nunique()
    print("\n--- Demographics Hyperparameters ---")
    print("NUM_AGES:", NUM_AGES)
    print("NUM_GENDERS:", NUM_GENDERS)
    print("NUM_ETHNICITIES:", NUM_ETHNICITIES)
    print("NUM_INSURANCES:", NUM_INSURANCES)
    NUM_LAB_FEATURES = len(lab_feature_columns)
    print("NUM_LAB_FEATURES (tokens):", NUM_LAB_FEATURES)

    behrt_demo = BEHRTModel_Demo(
        num_ages=NUM_AGES,
        num_genders=NUM_GENDERS,
        num_ethnicities=NUM_ETHNICITIES,
        num_insurances=NUM_INSURANCES,
        hidden_size=768
    ).to(device)
    behrt_lab = BEHRTModel_Lab(
        lab_token_count=NUM_LAB_FEATURES,
        hidden_size=768,
        nhead=8,
        num_layers=2
    ).to(device)

    multimodal_model = MultimodalTransformer(
        text_embed_size=768,
        behrt_demo=behrt_demo,
        behrt_lab=behrt_lab,
        device=device,
        hidden_size=512
    ).to(device)

    optimizer = torch.optim.Adam(multimodal_model.parameters(), lr=1e-5)
    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=2, verbose=True)
    mortality_pos_weight = get_pos_weight(df_filtered["short_term_mortality"], device)
    readmission_pos_weight = get_pos_weight(df_filtered["readmission_within_30_days"], device)
    criterion_mortality = FocalLoss(gamma=1, pos_weight=mortality_pos_weight, reduction='mean')
    criterion_readmission = FocalLoss(gamma=1, pos_weight=readmission_pos_weight, reduction='mean')

    num_epochs = 20
    for epoch in range(num_epochs):
        multimodal_model.train()
        running_loss = train_step(multimodal_model, dataloader, optimizer, device,
                                  criterion_mortality, criterion_readmission)
        epoch_loss = running_loss / len(dataloader)
        print(f"[Epoch {epoch+1}] Train Loss: {epoch_loss:.4f}")
        scheduler.step(epoch_loss)

    metrics = evaluate_model(multimodal_model, dataloader, device, threshold=0.5, print_eddi=True)
    print("\nFinal Evaluation Metrics (including subgroup-level EDDI):")
    for outcome in ["mortality", "readmission"]:
        m = metrics[outcome]
        print(f"{outcome.capitalize()} - AUC-ROC: {m['aucroc']:.4f}, AUPRC: {m['auprc']:.4f}, "
              f"F1: {m['f1']:.4f}, Recall: {m['recall']:.4f}, Precision: {m['precision']:.4f}")
    
    print("\nFinal Detailed EDDI Statistics:")
    eddi_stats = metrics["eddi_stats"]
    
    print("\nMortality EDDI Stats:")
    print("  Age subgroup EDDI      :", eddi_stats["mortality"]["age_subgroup_eddi"])
    print("  Aggregated age_eddi      : {:.4f}".format(eddi_stats["mortality"]["age_eddi"]))
    print("  Ethnicity subgroup EDDI  :", eddi_stats["mortality"]["ethnicity_subgroup_eddi"])
    print("  Aggregated ethnicity_eddi: {:.4f}".format(eddi_stats["mortality"]["ethnicity_eddi"]))
    print("  Insurance subgroup EDDI  :", eddi_stats["mortality"]["insurance_subgroup_eddi"])
    print("  Aggregated insurance_eddi: {:.4f}".format(eddi_stats["mortality"]["insurance_eddi"]))
    print("  Final Overall Mortality EDDI: {:.4f}".format(eddi_stats["mortality"]["final_EDDI"]))

    print("\nReadmission EDDI Stats:")
    print("  Age subgroup EDDI      :", eddi_stats["readmission"]["age_subgroup_eddi"])
    print("  Aggregated age_eddi      : {:.4f}".format(eddi_stats["readmission"]["age_eddi"]))
    print("  Ethnicity subgroup EDDI  :", eddi_stats["readmission"]["ethnicity_subgroup_eddi"])
    print("  Aggregated ethnicity_eddi: {:.4f}".format(eddi_stats["readmission"]["ethnicity_eddi"]))
    print("  Insurance subgroup EDDI  :", eddi_stats["readmission"]["insurance_subgroup_eddi"])
    print("  Aggregated insurance_eddi: {:.4f}".format(eddi_stats["readmission"]["insurance_eddi"]))
    print("  Final Overall Readmission EDDI: {:.4f}".format(eddi_stats["readmission"]["final_EDDI"]))

    print("Training complete.")

if __name__ == "__main__":
    train_pipeline()
