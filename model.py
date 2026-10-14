import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


class Ensemble:
    """Average of a regularised logistic regression and a shallow gradient-boosting model."""

    def __init__(self, seed: int = 42, mode: str = "ensemble"):
        self.mode = mode  # "logistic" | "gboost" | "ensemble"
        self.lr = make_pipeline(StandardScaler(), LogisticRegression(C=0.05, max_iter=500))
        self.gb = HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=200,
            l2_regularization=5.0, early_stopping=True, validation_fraction=0.15,
            n_iter_no_change=15, random_state=seed)
        self.cols = None

    def fit(self, X, y):
        self.cols = list(X.columns)
        Xv = X.fillna(0).values
        self.lr.fit(Xv, y)
        self.gb.fit(X.values, y)
        return self

    def predict_parts(self, X):
        X = X[self.cols]
        return self.lr.predict_proba(X.fillna(0).values)[:, 1], self.gb.predict_proba(X.values)[:, 1]

    def predict_proba(self, X):
        a, b = self.predict_parts(X)
        if self.mode == "logistic":
            return a
        if self.mode == "gboost":
            return b
        return (a + b) / 2
