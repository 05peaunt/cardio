import numpy as np
import sklearn


def get_class_prob(predictions_dict):
    true_dict = predictions_dict.get("target_true")
    pred_dict = predictions_dict.get("target_pred")
    if true_dict is None or pred_dict is None:
        raise ValueError("Each element of predictions list must be a dict with target_true and target_pred keys")
    return true_dict, pred_dict


def get_labels(predictions_list):
    true_labels = []
    pred_labels = []
    for predictions_dict in predictions_list:
        true_dict, pred_dict = get_class_prob(predictions_dict)
        true_labels.append(max(true_dict, key=true_dict.get))
        pred_labels.append(max(pred_dict, key=pred_dict.get))
    return np.array(true_labels), np.array(pred_labels)


def get_probs(predictions_list):
    true_probs = []
    pred_probs = []
    for predictions_dict in predictions_list:
        true_dict, pred_dict = get_class_prob(predictions_dict)
        true_probs.append([true_dict[key] for key in sorted(true_dict.keys())])
        pred_probs.append([pred_dict[key] for key in sorted(pred_dict.keys())])
    return np.array(true_probs), np.array(pred_probs)


def f1_score(predictions_list, average="macro", **kwargs):
    true_labels, pred_labels = get_labels(predictions_list)
    unique_labels = sorted(set(true_labels) | set(pred_labels))
    return sklearn.metrics.f1_score(true_labels, pred_labels, labels=unique_labels, average=average, **kwargs)


def auc(predictions_list, average="macro", **kwargs):
    return sklearn.metrics.roc_auc_score(*get_probs(predictions_list), average=average, **kwargs)


def classification_report(predictions_list, **kwargs):
    return sklearn.metrics.classification_report(*get_labels(predictions_list), **kwargs)


METRICS_DICT = {
    "f1_score": f1_score,
    "auc": auc,
    "classification_report": classification_report,
}


def calculate_metrics(metrics_list, predictions_list):
    metrics_res = []
    for metric in metrics_list:
        if isinstance(metric, str):
            metric_fn = METRICS_DICT.get(metric)
            if metric_fn is None:
                raise KeyError("Unknown metric name {}".format(metric))
        elif callable(metric):
            metric_fn = metric
        else:
            raise ValueError("Unknown metric type")
        metrics_res.append(metric_fn(predictions_list))
    return metrics_res
