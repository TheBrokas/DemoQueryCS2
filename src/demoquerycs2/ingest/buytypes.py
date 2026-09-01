"""Round buy classification from the team's carried equipment value."""

ECO_MAX = 5000       # <= : eco / save
SEMI_MAX = 16250     # <= : semi / half-buy


def classify(team_equipment_value: int) -> str:
    """Classify the equipment in play, including weapons saved from prior rounds."""
    if team_equipment_value <= ECO_MAX:
        return "eco"
    if team_equipment_value <= SEMI_MAX:
        return "semi"
    return "full"
