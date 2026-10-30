import { Component, type ReactNode } from "react";

interface Props {
	fallback: (error: Error, reset: () => void) => ReactNode;
	children: ReactNode;
}

interface State {
	error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
	state: State = { error: null };

	static getDerivedStateFromError(error: Error): State {
		return { error };
	}

	reset = () => {
		this.setState({ error: null });
	};

	render() {
		if (this.state.error) {
			return this.props.fallback(this.state.error, this.reset);
		}
		return this.props.children;
	}
}
