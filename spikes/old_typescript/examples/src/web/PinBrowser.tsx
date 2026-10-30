/**
 * Pin browser — lists all pins from the server and handles selection.
 * Uses plain HTTP (not fxtr scopes) to fetch the pin list.
 */

import { useEffect, useState } from "react";

export function PinBrowser({
	serverUrl,
	onSelect,
	selectedPin,
}: {
	serverUrl: string;
	onSelect: (pin: string) => void;
	selectedPin: string | null;
}) {
	const [pinNames, setPinNames] = useState<string[]>([]);
	const [loading, setLoading] = useState(true);

	useEffect(() => {
		(async () => {
			try {
				const res = await fetch(`${serverUrl}/pins`);
				const data = (await res.json()) as Record<string, string>;
				setPinNames(Object.keys(data));
			} catch (e) {
				console.error("Failed to fetch pins:", e);
			} finally {
				setLoading(false);
			}
		})();
	}, [serverUrl]);

	if (loading) return <p className="loading">Loading pins...</p>;

	return (
		<div>
			<h2>Pins</h2>
			<ul className="pin-list">
				{pinNames.map((pin) => (
					<li key={pin}>
						<button
							type="button"
							onClick={() => onSelect(pin)}
							style={{ fontWeight: pin === selectedPin ? "bold" : "normal" }}
						>
							{pin}
						</button>
					</li>
				))}
			</ul>
		</div>
	);
}
