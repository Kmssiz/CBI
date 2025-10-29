
import pandas as pd
import torch
import re
from transformers import DistilBertTokenizer, DistilBertForSequenceClassification
import os
import logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATHS = {
    'RSPowerBI': os.path.join(BASE_DIR, 'anomaly_detection', 'models', 'rspowerbi'),
    'RSPortal': os.path.join(BASE_DIR, 'anomaly_detection', 'models', 'rsportal'),
    'RSHostingService': os.path.join(BASE_DIR, 'anomaly_detection', 'models', 'rshostingservice')
}

MODEL_CACHE = {}
TOKENIZER_CACHE = {}
THRESHOLD_CACHE = {}

ANOMALY_PATTERNS = {
    'RSPowerBI': [
        {'category': 'Data Refresh Failure', 'pattern': r'\bole\s+db\s*error\b|login\s+timeout\b|failed\s+to\s+refresh\s+the\s+model\b', 'flags': re.IGNORECASE, 'likely_cause': 'Database connection timeout or credentials issue.'},
        {'category': 'Model Processing Error', 'pattern': r'\boutofmemoryexception\b|pbix\b', 'flags': re.IGNORECASE, 'likely_cause': 'Insufficient memory or corrupted PBIX file.'},
        {'category': 'Visual Rendering Issues', 'pattern': r'\brendering\s+engine\s+encountered\b|custom\s+visual\b|excessive\s+data\b', 'flags': re.IGNORECASE, 'likely_cause': 'Custom visual error or excessive data volume.'},
        {'category': 'Authentication Errors', 'pattern': r'\bfailed\s+to\s+authenticate\b', 'flags': re.IGNORECASE, 'likely_cause': 'Invalid user credentials or token expiration.'},
        {'category': 'Connectivity Warnings', 'pattern': r'\bretrying\s+connection\b|sqlserver\b', 'flags': re.IGNORECASE, 'likely_cause': 'Intermittent network issues or SQL Server downtime.'},
        {'category': 'Database Connectivity Error', 'pattern': r'\bodbc\b.*\bsql\s+server\s+does\s+not\s+exist\b|\baccess\s+denied\b', 'flags': re.IGNORECASE, 'likely_cause': 'SQL Server unavailable or access permissions denied.'},
        {'category': 'Data Reduction Error', 'pattern': r'\bdata\s+reduction\s+algorithm\b|\bgrouping\s+does\s+not\s+exist\b', 'flags': re.IGNORECASE, 'likely_cause': 'Invalid grouping or data reduction configuration.'},
        {'category': 'Invalid Data Source Error', 'pattern': r'\bno\s+connection\s+string\b|\binvaliddatasourceexception\b|\bdatabasename\s+is\s+null\s+or\s+empty\b', 'flags': re.IGNORECASE, 'likely_cause': 'Missing or invalid data source connection string.'},
        {'category': 'Query Processing Error', 'pattern': r'\border\s+by\b.*\bis\s+ignored\b', 'flags': re.IGNORECASE, 'likely_cause': 'Invalid query syntax or unsupported ORDER BY clause.'},
        {'category': 'Data Processing Limit Error', 'pattern': r'\bexceeds\s+maximum\s+allowed\s+number\s+of\s+intersections\b', 'flags': re.IGNORECASE, 'likely_cause': 'Query exceeds allowed intersection limit.'},
        {'category': 'Network Service Error', 'pattern': r'\bowin\s+pipeline\b|\bnetwork\s+name\s+is\s+no\s+longer\s+available\b', 'flags': re.IGNORECASE, 'likely_cause': 'Network service interruption or OWIN pipeline failure.'},
        {'category': 'RLS Authorization Error', 'pattern': r'\brlsnotauthorizedformodelexception\b', 'flags': re.IGNORECASE, 'likely_cause': 'Row-Level Security misconfiguration or unauthorized access.'},
        {'category': 'Database Not Found Error', 'pattern': r'\bdatabasenotfoundexception\b|\bcouldn\s+t\s+find\s+database\b', 'flags': re.IGNORECASE, 'likely_cause': 'Database not registered or deleted on the server.'},
    ],
    'RSPortal': [
        
         {
            'category': 'Authentication Failure',
            'pattern': r'\b(http\s+401|unauthorized|unmatched\s+or\s+no\s+authentication\s+scheme|UnknownUserNameException|n\'est\s+pas\s+reconnu)\b',
            'flags': re.IGNORECASE,
            'likely_cause': 'Missing, invalid, or unrecognized user/group authentication token/scheme.'
        },
        {
            'category': 'Missing Resource (404)',
            'pattern': r'\b(http\s+404|not\s+found|ItemNotFoundException|cannot\s+be\s+found)\b',
            'flags': re.IGNORECASE,
            'likely_cause': 'Requested resource or item does not exist on the server.'
        },
        {
            'category': 'Access Denied',
            'pattern': r'\b(http\s+403|forbidden|AccessDeniedException|insufficient\s+(permissions|autorisations))\b',
            'flags': re.IGNORECASE,
            'likely_cause': 'User lacks permission to access the resource or perform the operation.'
        },
        {
            'category': 'Upload/Download Errors',
            'pattern': r'\b(upload\s+failed|file\s+size)\b',
            'flags': re.IGNORECASE,
            'likely_cause': 'File size limit exceeded or upload/download error.'
        },
        {
            'category': 'Session/Cookie Issues',
            'pattern': r'\b(invalid\s+anti\s+forgery|machinekey)\b',
            'flags': re.IGNORECASE,
            'likely_cause': 'Invalid session token or cookie mismatch.'
        },
        {
            'category': 'Performance Warnings',
            'pattern': r'\b(request\s+took|render\s+page)\b',
            'flags': re.IGNORECASE,
            'likely_cause': 'High server load or slow page rendering.'
        },
    ],
    'RSHostingService': [
        {'category': 'Subscription Failure', 'pattern': r'\bsubscription\s+delivery\s+failed\b|smtp\b', 'flags': re.IGNORECASE, 'likely_cause': 'SMTP server issue or subscription misconfiguration.'},
        {'category': 'Service Crash/Restart', 'pattern': r'\bservice\s+terminated\b|unhandled\s+exception\b', 'flags': re.IGNORECASE, 'likely_cause': 'Unexpected service error or crash.'},
        {'category': 'Rendering/Queue Timeouts', 'pattern': r'\brender\s+job\s+aborted\b|insufficient\s+threads\b', 'flags': re.IGNORECASE, 'likely_cause': 'Insufficient resources or job timeout.'},
        {'category': 'Cache Cleanup Errors', 'pattern': r'\bcannot\s+delete\s+temp\b|access\s+denied\b', 'flags': re.IGNORECASE, 'likely_cause': 'Permission issue or locked temporary files.'},
        {'category': 'Connectivity Warnings', 'pattern': r'\bfailed\s+to\s+connect\b|reportserver\s+db\b', 'flags': re.IGNORECASE, 'likely_cause': 'Database connection failure or network issue.'},
    ]
}

def load_model_and_tokenizer(dataset_name):
    if dataset_name not in MODEL_PATHS:
        raise ValueError(f"Invalid dataset_name: {dataset_name}")
    model_path = MODEL_PATHS[dataset_name]
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model directory not found: {model_path}")
    if dataset_name in MODEL_CACHE:
        logger.info(f"Using cached model for {dataset_name}")
        return TOKENIZER_CACHE[dataset_name], MODEL_CACHE[dataset_name], THRESHOLD_CACHE[dataset_name]
    logger.info(f"Loading model from {model_path}")
    tokenizer = DistilBertTokenizer.from_pretrained(model_path)
    model = DistilBertForSequenceClassification.from_pretrained(model_path)
    device = torch.device('cpu')
    model.to(device)
    logger.info(f"Model moved to device: {device}")
    threshold_path = os.path.join(model_path, 'threshold.txt')
    if not os.path.exists(threshold_path):
        raise FileNotFoundError(f"Threshold file not found: {threshold_path}")
    with open(threshold_path, 'r') as f:
        threshold = float(f.read())
    TOKENIZER_CACHE[dataset_name] = tokenizer
    MODEL_CACHE[dataset_name] = model
    THRESHOLD_CACHE[dataset_name] = threshold
    return tokenizer, model, threshold

def classify_anomaly(message, dataset_name):
    if dataset_name not in ANOMALY_PATTERNS:
        return 'Other', ''
    for entry in ANOMALY_PATTERNS[dataset_name]:
        if re.search(entry['pattern'], message, entry['flags']):
            return entry['category'], entry['likely_cause']
    return 'Other', 'Unknown cause; review log message for details.'

def preprocess_log_lines(lines):
    logs = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('-----'):
            continue
        parts = line.split('|', 3)
        if len(parts) < 4:
            continue
        timestamp = parts[0].strip()
        level = parts[1].strip()
        message = parts[3].strip()
        cleaned_message = re.sub(r'[^\w\s]', ' ', message)
        cleaned_message = re.sub(r'\s+', ' ', cleaned_message).strip().lower()
        if cleaned_message:
            logs.append({
                'timestamp': timestamp,
                'level': level,
                'message': cleaned_message,
                'raw_message': message
            })
    return logs

def process_log_chunk(lines, dataset_name, batch_size=32):
    logs = preprocess_log_lines(lines)
    if not logs:
        return []
    tokenizer, model, threshold = load_model_and_tokenizer(dataset_name)
    device = torch.device('cpu')
    all_results = []
    for i in range(0, len(logs), batch_size):
        batch_logs = logs[i:i + batch_size]
        messages = [log['message'] for log in batch_logs]
        inputs = tokenizer(messages, padding=True, truncation=True, max_length=512, return_tensors='pt')
        inputs = {k: v.to(device) for k, v in inputs.items()}
        logger.info(f"Processing batch {i//batch_size + 1} on device: {device}")
        model.eval()
        with torch.no_grad():
            outputs = model(**inputs)
            probs = torch.softmax(outputs.logits, dim=1)[:, 1].numpy()
            predictions = (probs > threshold).astype(int)
        batch_results = [
            {
                'timestamp': log['timestamp'],
                'level': log['level'],
                'message': log['raw_message'],
                'is_anomaly': bool(pred),
                'probability': float(prob),
                'category': classify_anomaly(log['raw_message'], dataset_name)[0] if bool(pred) else '',
                'likely_cause': classify_anomaly(log['raw_message'], dataset_name)[1] if bool(pred) else ''
            }
            for log, pred, prob in zip(batch_logs, predictions, probs)
        ]
        all_results.extend(batch_results)
    return all_results