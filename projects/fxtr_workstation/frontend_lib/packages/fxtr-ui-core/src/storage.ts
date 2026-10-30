/**
 * In-memory `Storage` for tests / SSR.
 *
 * The connector persists through the standard Web Storage `Storage`
 * interface (`localStorage` / `sessionStorage` pass in directly); the
 * one variant with no built-in is an in-memory store, so core provides
 * this `Map`-backed implementation. It satisfies the full `Storage`
 * surface, so it can stand in anywhere a real `Storage` is expected
 * (connector persistence + PanelViewer layout persistence share one
 * handle). Not multi-tab safe — it's per-instance.
 */
export function memoryStorage(): Storage {
	const map = new Map<string, string>();
	const storage: Storage = {
		get length(): number {
			return map.size;
		},
		clear(): void {
			map.clear();
		},
		getItem(key: string): string | null {
			return map.has(key) ? (map.get(key) as string) : null;
		},
		key(index: number): string | null {
			return Array.from(map.keys())[index] ?? null;
		},
		removeItem(key: string): void {
			map.delete(key);
		},
		setItem(key: string, value: string): void {
			map.set(key, String(value));
		},
	};
	return storage;
}
