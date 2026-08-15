"""Cost analysis: anomaly detection and rightsizing recommendations."""

from .anomaly import Anomaly, DailyCost, detect
from .rightsizing import Recommendation, Usage, Verdict, recommend, summarise

__version__ = "0.4.0"

__all__ = [
    "Anomaly",
    "DailyCost",
    "Recommendation",
    "Usage",
    "Verdict",
    "detect",
    "recommend",
    "summarise",
]
