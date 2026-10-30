/**
 * Dimension color palette — shared between server (SVG) and client (badges).
 * Same order as the Python report's DIM_COLORS.
 */

export const DIM_COLORS = [
	"#1f77b4",
	"#ff7f0e",
	"#2ca02c",
	"#d62728",
	"#9467bd",
	"#8c564b",
	"#e377c2",
	"#7f7f7f",
	"#bcbd22",
	"#17becf",
	"#aec7e8",
];

const DIMENSION_ORDER = [
	"mental_health_presentation",
	"crisis_acuity",
	"conversation_goals",
	"how_user_positions_assistant",
	"insight_and_self_awareness",
	"relationship_to_treatment",
	"precipitating_event",
	"life_situation_and_demographics",
	"social_connectedness_and_situation",
	"engagement_style",
	"conversation_flow_and_continuity",
];

const colorMap = new Map(
	DIMENSION_ORDER.map((dim, i) => [dim, DIM_COLORS[i % DIM_COLORS.length] ?? "#999"]),
);

export function dimColor(dimensionName: string): string {
	return colorMap.get(dimensionName) ?? "#999";
}
