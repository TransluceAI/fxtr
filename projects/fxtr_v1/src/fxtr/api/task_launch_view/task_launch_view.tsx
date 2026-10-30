// /// package
// dependencies = [
//     "react",
//     "react-dom",
//     "picoview@monorepo:lib/picoview/frontend_lib",
//     "d3@^7.8.5",
//     "d3-dag@^0.9.0",
//     "@types/d3@^7.4.0",
// ]
// ///

import React, { useEffect, useRef } from "react";
import { usePyViewData, withBasicConnectionStatus } from "picoview";
import * as d3 from "d3";
import { dagStratify, sugiyama, layeringLongestPath, decrossTwoLayer, coordQuad } from "d3-dag";

interface PlanData {
  cid: string;
  type_name: string;
  status: string;
  status_color: string;
  message: string;
}

interface TaskData {
  cid: string;
  name: string;
  status: string;
  status_color: string;
  message: string;
}

interface SummaryData {
  completed: number;
  running: number;
  pending: number;
  cancelled: number;
  failed: number;
  total: number;
}

interface DagNode {
  id: string;
  type: string; // "plan" or "task"
  label: string;
  status: string;
  status_color: string;
}

interface DagEdge {
  source: string;
  target: string;
}

interface ViewData {
  plans: PlanData[];
  tasks: TaskData[];
  plan_summary: SummaryData;
  task_summary: SummaryData;
  dag_nodes: DagNode[];
  dag_edges: DagEdge[];
}

function ProgressBar({ summary }: { summary: SummaryData }) {
  const { completed, running, pending, cancelled, failed, total } = summary;

  if (total === 0) {
    return (
      <div style={{ color: "#6b7280", fontSize: "14px" }}>
        No items
      </div>
    );
  }

  // Calculate percentages
  const completedPct = (completed / total) * 100;
  const runningPct = (running / total) * 100;
  const pendingPct = (pending / total) * 100;
  const cancelledPct = (cancelled / total) * 100;
  const failedPct = (failed / total) * 100;

  return (
    <div style={{ marginTop: "12px" }}>
      {/* Visual bar */}
      <div
        style={{
          width: "100%",
          height: "8px",
          backgroundColor: "#f3f4f6",
          borderRadius: "4px",
          overflow: "hidden",
          display: "flex"
        }}
      >
        {completed > 0 && (
          <div
            style={{
              width: `${completedPct}%`,
              height: "100%",
              backgroundColor: "#22c55e"
            }}
          />
        )}
        {running > 0 && (
          <div
            style={{
              width: `${runningPct}%`,
              height: "100%",
              backgroundColor: "#3b82f6"
            }}
          />
        )}
        {pending > 0 && (
          <div
            style={{
              width: `${pendingPct}%`,
              height: "100%",
              backgroundColor: "#9ca3af"
            }}
          />
        )}
        {cancelled > 0 && (
          <div
            style={{
              width: `${cancelledPct}%`,
              height: "100%",
              backgroundColor: "#f59e0b"
            }}
          />
        )}
        {failed > 0 && (
          <div
            style={{
              width: `${failedPct}%`,
              height: "100%",
              backgroundColor: "#ef4444"
            }}
          />
        )}
      </div>

      {/* Status counts */}
      <div
        style={{
          display: "flex",
          gap: "12px",
          fontSize: "12px",
          color: "#6b7280",
          marginTop: "4px",
          flexWrap: "wrap"
        }}
      >
        {completed > 0 && <span>✓{completed}</span>}
        {running > 0 && <span>▶{running}</span>}
        {pending > 0 && <span>⏸{pending}</span>}
        {cancelled > 0 && <span>⊘{cancelled}</span>}
        {failed > 0 && <span>✗{failed}</span>}
        <span style={{ marginLeft: "auto" }}>Total: {total}</span>
      </div>
    </div>
  );
}

function ItemTable({ items, title }: { items: Array<PlanData | TaskData>; title: string }) {
  return (
    <div
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: "8px",
        overflow: "hidden"
      }}
    >
      <div
        style={{
          backgroundColor: "#f9fafb",
          padding: "12px 16px",
          fontWeight: "600",
          color: "#374151",
          borderBottom: "1px solid #e5e7eb"
        }}
      >
        {title}
      </div>
      <div style={{ maxHeight: "400px", overflowY: "auto" }}>
        {items.map((item, index) => (
          <div
            key={item.cid}
            style={{
              padding: "12px 16px",
              borderBottom: index < items.length - 1 ? "1px solid #f3f4f6" : "none",
              display: "grid",
              gridTemplateColumns: "100px 1fr 80px",
              gap: "12px",
              alignItems: "start",
              fontSize: "14px"
            }}
          >
            <div
              style={{
                fontFamily: "monospace",
                fontSize: "12px",
                color: "#6b7280",
                wordBreak: "break-all"
              }}
            >
              {item.cid}
            </div>
            <div>
              <div style={{ fontWeight: "500", marginBottom: "4px" }}>
                {"type_name" in item ? item.type_name : item.name}
              </div>
              {item.message && (
                <div
                  style={{
                    fontSize: "12px",
                    color: "#6b7280",
                    whiteSpace: "pre-wrap",
                    maxWidth: "400px",
                    overflow: "hidden",
                    textOverflow: "ellipsis"
                  }}
                >
                  {item.message}
                </div>
              )}
            </div>
            <div
              style={{
                color: item.status_color,
                fontWeight: "500",
                textAlign: "right"
              }}
            >
              {item.status}
            </div>
          </div>
        ))}
        {items.length === 0 && (
          <div
            style={{
              padding: "24px",
              textAlign: "center",
              color: "#6b7280",
              fontStyle: "italic"
            }}
          >
            No items
          </div>
        )}
      </div>
    </div>
  );
}

function DagVisualization({ nodes, edges }: { nodes: DagNode[], edges: DagEdge[] }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!svgRef.current || nodes.length === 0) return;

    // Clear the SVG
    d3.select(svgRef.current).selectAll("*").remove();

    // Create hierarchical data structure for d3-dag
    const dagData = nodes.map(node => ({
      id: node.id,
      parentIds: edges.filter(e => e.target === node.id).map(e => e.source),
      ...node
    }));

    try {
      // Create DAG from data
      const dag = dagStratify()(dagData);

      // Set up layout with size constraints
      const containerWidth = containerRef.current?.clientWidth || 800;
      const minWidth = Math.max(containerWidth - 80, 600); // Leave some margin

      const layout = sugiyama()
        .layering(layeringLongestPath())
        .decross(decrossTwoLayer())
        .coord(coordQuad())
        .size([minWidth, 400]); // Set desired width and height

      const { width, height } = layout(dag);

      // Set up SVG dimensions with padding
      const padding = 40;
      const svgWidth = Math.max(width + 2 * padding, containerWidth);
      const svgHeight = height + 2 * padding;

      const svg = d3.select(svgRef.current)
        .attr("width", svgWidth)
        .attr("height", svgHeight);

      // Create a group for the DAG with padding
      const g = svg.append("g")
        .attr("transform", `translate(${padding}, ${padding})`);

      // Create arrow markers
      svg.append("defs").selectAll("marker")
        .data(["arrow"])
        .enter().append("marker")
        .attr("id", "arrow")
        .attr("viewBox", "0 -5 10 10")
        .attr("refX", 15)
        .attr("refY", 0)
        .attr("markerWidth", 6)
        .attr("markerHeight", 6)
        .attr("orient", "auto")
        .append("path")
        .attr("d", "M0,-5L10,0L0,5")
        .attr("fill", "#999");

      // Draw edges
      g.selectAll(".link")
        .data(dag.links())
        .enter().append("line")
        .attr("class", "link")
        .attr("x1", (d: any) => d.source.x)
        .attr("y1", (d: any) => d.source.y)
        .attr("x2", (d: any) => d.target.x)
        .attr("y2", (d: any) => d.target.y)
        .attr("stroke", "#999")
        .attr("stroke-width", 2)
        .attr("marker-end", "url(#arrow)");

      // Draw nodes
      const nodeGroups = g.selectAll(".node")
        .data(dag.descendants())
        .enter().append("g")
        .attr("class", "node")
        .attr("transform", d => `translate(${d.x}, ${d.y})`);

      // Helper function to wrap text
      function wrapText(label: string) {
        const words = label.split(/\s+/);
        const lines: string[] = [];
        let currentLine = "";

        for (const word of words) {
          if ((currentLine + word).length <= 20) {
            currentLine = currentLine ? currentLine + " " + word : word;
          } else {
            if (currentLine) lines.push(currentLine);
            currentLine = word;
          }
        }
        if (currentLine) lines.push(currentLine);

        // If single word is too long, break it
        if (lines.length === 1 && lines[0].length > 20) {
          const longLine = lines[0];
          lines.length = 0;
          for (let i = 0; i < longLine.length; i += 20) {
            lines.push(longLine.substring(i, i + 20));
          }
        }

        return lines;
      }

      // Add shapes based on node type
      nodeGroups.each(function(d: any) {
        const group = d3.select(this);

        // Calculate text dimensions for dynamic sizing
        const lines = wrapText(d.data.label);
        const lineCount = Math.max(1, lines.length);
        const textHeight = lineCount * 12; // 12px per line
        const maxLineLength = Math.max(...lines.map(line => line.length));
        const textWidth = Math.max(40, maxLineLength * 6); // ~6px per character

        if (d.data.type === "task") {
          // Square for tasks - adjust size based on text
          const rectWidth = Math.max(50, textWidth + 10);
          const rectHeight = Math.max(35, textHeight + 10);
          group.append("rect")
            .attr("x", -rectWidth/2)
            .attr("y", -rectHeight/2)
            .attr("width", rectWidth)
            .attr("height", rectHeight)
            .attr("fill", d.data.status_color)
            .attr("stroke", "#333")
            .attr("stroke-width", 2);
        } else {
          // Rounded rectangle for plans to accommodate text better
          const rectWidth = Math.max(50, textWidth + 10);
          const rectHeight = Math.max(35, textHeight + 10);
          group.append("rect")
            .attr("x", -rectWidth/2)
            .attr("y", -rectHeight/2)
            .attr("width", rectWidth)
            .attr("height", rectHeight)
            .attr("rx", rectHeight/2) // Make it pill-shaped
            .attr("ry", rectHeight/2)
            .attr("fill", d.data.status_color)
            .attr("stroke", "#333")
            .attr("stroke-width", 2);
        }

        // Add text labels with wrapping
        const text = group.append("text")
          .attr("text-anchor", "middle")
          .style("font-size", "10px")
          .style("font-weight", "bold")
          .style("fill", "black");

        // Add each line as a tspan
        lines.forEach((line, i) => {
          text.append("tspan")
            .attr("x", 0)
            .attr("dy", i === 0 ? "0.35em" : "1.2em")
            .text(line);
        });

        // Add tooltip
        group.append("title")
          .text(`${d.data.type}: ${d.data.label} (${d.data.status})`);
      });

    } catch (error) {
      console.warn("DAG visualization error:", error);
      // Show fallback message
      d3.select(svgRef.current)
        .append("text")
        .attr("x", "50%")
        .attr("y", "50%")
        .attr("text-anchor", "middle")
        .attr("dominant-baseline", "middle")
        .style("fill", "#666")
        .text("DAG visualization unavailable");
    }
  }, [nodes, edges]);

  return (
    <div
      ref={containerRef}
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: "8px",
        padding: "16px",
        backgroundColor: "#fff",
        overflow: "auto",
        minHeight: "300px",
        width: "100%"
      }}
    >
      <div
        style={{
          fontWeight: "600",
          color: "#374151",
          marginBottom: "16px"
        }}
      >
        Task & Plan Dependency Graph
      </div>
      <svg ref={svgRef} style={{ width: "100%", minHeight: "250px", display: "block" }} />
    </div>
  );
}

export function TaskLaunchView() {
  const viewData = usePyViewData<ViewData>();

  if (!viewData) {
    return (
      <div
        style={{
          padding: "20px",
          fontFamily: "system-ui, sans-serif",
          textAlign: "center"
        }}
      >
        Loading...
      </div>
    );
  }

  const { plans, tasks, plan_summary, task_summary, dag_nodes, dag_edges } = viewData;

  return (
    <div
      style={{
        padding: "20px",
        fontFamily: "system-ui, sans-serif",
        backgroundColor: "#fafafa",
        minHeight: "100vh"
      }}
    >
      <header style={{ marginBottom: "24px" }}>
        <h1
          style={{
            fontSize: "24px",
            fontWeight: "700",
            color: "#111827",
            margin: "0 0 16px 0"
          }}
        >
          Task Launch Progress
        </h1>
        <div
          style={{
            fontSize: "14px",
            color: "#6b7280"
          }}
        >
          Monitoring {plans.length} plans and {tasks.length} tasks
        </div>
      </header>

      {/* DAG Visualization */}
      <div style={{ marginBottom: "24px" }}>
        <DagVisualization nodes={dag_nodes} edges={dag_edges} />
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "24px",
          alignItems: "start"
        }}
      >
        {/* Plans column */}
        <div>
          <ItemTable items={plans} title="Plans" />
          <ProgressBar summary={plan_summary} />
        </div>

        {/* Tasks column */}
        <div>
          <ItemTable items={tasks} title="Tasks" />
          <ProgressBar summary={task_summary} />
        </div>
      </div>
    </div>
  );
}

export default withBasicConnectionStatus(TaskLaunchView);
