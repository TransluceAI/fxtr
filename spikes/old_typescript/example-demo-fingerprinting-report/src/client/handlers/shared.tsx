/**
 * Shared components used by multiple handler files.
 */

export function MessageBubbles({
	messages,
	fontSize,
}: {
	messages: Array<{ role: string; content: string }>;
	fontSize: number;
}) {
	const userBubble: React.CSSProperties = {
		padding: `${fontSize * 0.8}px ${fontSize}px`,
		borderRadius: "12px 12px 4px 12px",
		backgroundColor: "#e3f2fd",
		border: "1px solid #bbdefb",
		fontSize: `${fontSize}px`,
		lineHeight: 1.6,
		whiteSpace: "pre-wrap",
		maxWidth: "90%",
		alignSelf: "flex-end",
	};

	const assistantBubble: React.CSSProperties = {
		padding: `${fontSize * 0.8}px ${fontSize}px`,
		borderRadius: "12px 12px 12px 4px",
		backgroundColor: "#f5f5f5",
		border: "1px solid #e0e0e0",
		fontSize: `${fontSize}px`,
		lineHeight: 1.6,
		whiteSpace: "pre-wrap",
		maxWidth: "90%",
		alignSelf: "flex-start",
	};

	const roleTag: React.CSSProperties = {
		fontSize: `${Math.max(9, fontSize - 3)}px`,
		fontWeight: 600,
		color: "#888",
		marginBottom: "3px",
		textTransform: "uppercase",
	};

	return (
		<div style={{ display: "flex", flexDirection: "column", gap: `${fontSize * 0.8}px` }}>
			{messages.map((msg, i) => (
				<div key={`${i}-${msg.role}`} style={msg.role === "user" ? userBubble : assistantBubble}>
					<div style={roleTag}>{msg.role}</div>
					{msg.content}
				</div>
			))}
		</div>
	);
}
