from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class RepairRule:
    keywords: Tuple[str, ...]
    label: str
    min_cost: float
    max_cost: float
    resale_penalty_percent: float
    severity: str


IPHONE_REPAIR_RULES: Tuple[RepairRule, ...] = (
    RepairRule(
        keywords=("vetro posteriore", "retro crepato", "back glass", "crepa sul retro"),
        label="Vetro posteriore danneggiato",
        min_cost=50,
        max_cost=120,
        resale_penalty_percent=12,
        severity="media",
    ),
    RepairRule(
        keywords=("display rotto", "schermo rotto", "display crepato", "schermo crepato"),
        label="Display danneggiato",
        min_cost=90,
        max_cost=250,
        resale_penalty_percent=25,
        severity="alta",
    ),
    RepairRule(
        keywords=("fotocamera", "camera lens", "lente fotocamera"),
        label="Modulo o lente fotocamera da verificare",
        min_cost=40,
        max_cost=180,
        resale_penalty_percent=12,
        severity="media",
    ),
    RepairRule(
        keywords=("cornice piegata", "telaio piegato", "scocca piegata"),
        label="Telaio/scocca deformati",
        min_cost=100,
        max_cost=300,
        resale_penalty_percent=30,
        severity="alta",
    ),
    RepairRule(
        keywords=("segni d'uso", "graffi", "ammaccature", "usura"),
        label="Usura estetica",
        min_cost=0,
        max_cost=30,
        resale_penalty_percent=5,
        severity="bassa",
    ),
)

IPHONE_CHECKLIST: Tuple[str, ...] = (
    "Verificare che l'iPhone si accenda e completi l'avvio.",
    "Controllare che non sia presente un blocco iCloud/Activation Lock.",
    "Verificare IMEI e assenza di blocchi operatore o denuncia.",
    "Controllare stato batteria e messaggi su componenti non originali.",
    "Provare Face ID o Touch ID.",
    "Provare touch su tutta la superficie dello schermo.",
    "Controllare True Tone e luminosità uniforme.",
    "Provare fotocamere, messa a fuoco, flash e video.",
    "Provare microfono, altoparlanti e chiamata.",
    "Provare ricarica via cavo e, se presente, ricarica wireless.",
    "Controllare Wi‑Fi, Bluetooth, GPS e rete mobile.",
    "Verificare che il dispositivo venga inizializzato davanti all'acquirente.",
)

IPHONE_QUESTIONS: Tuple[str, ...] = (
    "Il telefono è mai stato riparato o aperto?",
    "Display, batteria o fotocamere sono originali?",
    "Face ID/Touch ID funziona correttamente?",
    "Qual è la percentuale di capacità massima della batteria?",
    "Sono presenti fattura, scontrino o prova d'acquisto?",
    "È mai caduto in acqua o ha subito infiltrazioni?",
    "L'IMEI è libero e il telefono è completamente scollegato da iCloud?",
)

IPHONE_NO_BUY: Tuple[str, ...] = (
    "Blocco iCloud o impossibilità di inizializzare il dispositivo.",
    "IMEI bloccato, non verificabile o incongruente.",
    "Face ID non funzionante senza forte sconto e diagnosi certa.",
    "Display con touch difettoso, linee, aloni o burn-in.",
    "Segni di infiltrazione o danni da liquido.",
    "Componenti non originali non dichiarati.",
    "Venditore che rifiuta prova completa o reset davanti all'acquirente.",
)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _collect_visible_text(vision_result: Dict[str, Any]) -> str:
    parts: List[str] = [
        str(vision_result.get("notes") or ""),
        str(vision_result.get("visible_condition") or ""),
    ]
    parts.extend(str(v) for v in vision_result.get("visible_defects") or [])
    parts.extend(str(v) for v in vision_result.get("counterfeit_or_fraud_signals") or [])
    return _norm(" ".join(parts))


def _matched_repairs(vision_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    text = _collect_visible_text(vision_result)
    matches: List[Dict[str, Any]] = []

    for rule in IPHONE_REPAIR_RULES:
        if any(keyword in text for keyword in rule.keywords):
            matches.append(
                {
                    "issue": rule.label,
                    "min_cost": rule.min_cost,
                    "max_cost": rule.max_cost,
                    "resale_penalty_percent": rule.resale_penalty_percent,
                    "severity": rule.severity,
                }
            )

    return matches


def _risk_score(vision_result: Dict[str, Any], repairs: List[Dict[str, Any]]) -> int:
    score = 0
    condition = _norm(vision_result.get("visible_condition"))
    consistency = _norm(vision_result.get("text_image_consistency"))

    if condition == "damaged":
        score += 30
    elif condition == "fair":
        score += 15

    if consistency == "inconsistent":
        score += 35
    elif consistency == "partially_consistent":
        score += 12

    for repair in repairs:
        severity = repair["severity"]
        score += {"bassa": 5, "media": 12, "alta": 25}.get(severity, 10)

    fraud_signals = vision_result.get("counterfeit_or_fraud_signals") or []
    score += min(len(fraud_signals) * 8, 24)

    return min(score, 100)


def _condition_factor(vision_result: Dict[str, Any]) -> float:
    return {
        "new": 0.98,
        "like_new": 0.94,
        "good": 0.88,
        "fair": 0.78,
        "damaged": 0.65,
        "unknown": 0.72,
    }.get(_norm(vision_result.get("visible_condition")), 0.72)


def build_knowledge_report(
    vision_result: Dict[str, Any],
    asking_price: Optional[float] = None,
    market_value: Optional[float] = None,
    target_margin: float = 80.0,
    transaction_costs: float = 25.0,
) -> Dict[str, Any]:
    """
    Trasforma l'analisi visiva in una valutazione operativa.

    Nota:
    - la stima economica è prudenziale;
    - non sostituisce test dal vivo;
    - il prezzo massimo è disponibile solo se viene fornito market_value.
    """
    category = _norm(vision_result.get("category"))
    brand = _norm(vision_result.get("brand"))

    if category not in {"smartphone", "telefono", "cellulare"} or brand != "apple":
        return {
            "supported": False,
            "knowledge_version": "v1",
            "message": "Categoria non ancora coperta dal Knowledge Engine v1.",
        }

    repairs = _matched_repairs(vision_result)
    repair_min = sum(float(row["min_cost"]) for row in repairs)
    repair_max = sum(float(row["max_cost"]) for row in repairs)
    resale_penalty = min(
        sum(float(row["resale_penalty_percent"]) for row in repairs),
        45.0,
    )
    risk_score = _risk_score(vision_result, repairs)

    maximum_buy_price: Optional[float] = None
    prudent_resale_value: Optional[float] = None
    estimated_margin: Optional[float] = None
    roi: Optional[float] = None

    if isinstance(market_value, (int, float)) and market_value > 0:
        prudent_resale_value = (
            float(market_value)
            * _condition_factor(vision_result)
            * (1 - resale_penalty / 100)
        )

        maximum_buy_price = max(
            0.0,
            prudent_resale_value
            - repair_max
            - transaction_costs
            - target_margin,
        )

        if isinstance(asking_price, (int, float)) and asking_price > 0:
            estimated_margin = (
                prudent_resale_value
                - float(asking_price)
                - repair_max
                - transaction_costs
            )
            roi = estimated_margin / float(asking_price) * 100

    recognition_confidence = int(vision_result.get("recognition_confidence") or 0)
    condition_confidence = int(vision_result.get("condition_confidence") or 0)

    buy_score = 50
    buy_score += round((recognition_confidence - 50) * 0.20)
    buy_score += round((condition_confidence - 50) * 0.15)
    buy_score -= round(risk_score * 0.45)

    if isinstance(estimated_margin, (int, float)):
        buy_score += min(20, max(-20, round(estimated_margin / 10)))
    if isinstance(roi, (int, float)):
        buy_score += min(15, max(-15, round(roi / 5)))

    buy_score = max(0, min(100, buy_score))

    if risk_score >= 70:
        verdict = "SCARTA"
    elif maximum_buy_price is None:
        verdict = "VERIFICA E QUOTA"
    elif asking_price is None:
        verdict = "MANCA PREZZO"
    elif float(asking_price) <= maximum_buy_price and buy_score >= 65:
        verdict = "COMPRA DOPO VERIFICA"
    elif float(asking_price) <= maximum_buy_price * 1.15:
        verdict = "TRATTA"
    else:
        verdict = "SCARTA O OFFRI MOLTO MENO"

    return {
        "supported": True,
        "knowledge_version": "v1",
        "repair_items": repairs,
        "repair_cost_min": round(repair_min, 2),
        "repair_cost_max": round(repair_max, 2),
        "resale_penalty_percent": round(resale_penalty, 1),
        "risk_score": risk_score,
        "prudent_resale_value": (
            round(prudent_resale_value, 2)
            if prudent_resale_value is not None else None
        ),
        "maximum_buy_price": (
            round(maximum_buy_price, 2)
            if maximum_buy_price is not None else None
        ),
        "estimated_margin": (
            round(estimated_margin, 2)
            if estimated_margin is not None else None
        ),
        "roi": round(roi, 1) if roi is not None else None,
        "buy_score": buy_score,
        "verdict": verdict,
        "checklist": list(IPHONE_CHECKLIST),
        "questions_for_seller": list(IPHONE_QUESTIONS),
        "do_not_buy_if": list(IPHONE_NO_BUY),
    }
