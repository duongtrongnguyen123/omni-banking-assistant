import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, act } from "@testing-library/react";
import {
  SlashPalette,
  buildMessageFromSlash,
  SLASH_COMMANDS,
  type SlashCommand,
} from "../../src/components/SlashPalette";

// Test harness — mirrors how App.tsx drives the palette: input value
// controls open/query, Enter on the input triggers the active pick.
function Harness({ onPick }: { onPick: (cmd: SlashCommand, raw: string) => void }) {
  const [value, setValue] = useState("");
  const open = value.startsWith("/");
  const query = open ? value.slice(1) : "";
  return (
    <div>
      <SlashPalette
        open={open}
        query={query}
        rawInput={value}
        onPick={(cmd, raw) => {
          onPick(cmd, raw);
          // emulate App.tsx clearing the input on send
          setValue("");
        }}
        onClose={() => setValue("")}
      />
      <input
        aria-label="input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
    </div>
  );
}

describe("SlashPalette", () => {
  it("renders all commands when open with empty query", () => {
    const onPick = vi.fn();
    render(
      <SlashPalette
        open
        query=""
        rawInput="/"
        onPick={onPick}
        onClose={() => {}}
      />,
    );
    for (const cmd of SLASH_COMMANDS) {
      expect(screen.getByText(`/${cmd.key}`)).toBeInTheDocument();
    }
  });

  it("filters down to the transfer command when user types /tr", async () => {
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    const input = screen.getByLabelText("input") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "/tr" } });

    // Transfer row visible.
    expect(screen.getByText("/transfer")).toBeInTheDocument();
    // Balance row hidden (it doesn't match "tr").
    expect(screen.queryByText("/balance")).not.toBeInTheDocument();
  });

  it("invokes onPick with the highlighted command when Enter is pressed", async () => {
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    const input = screen.getByLabelText("input") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "/tr" } });

    // Global keydown handler is wired on window via useEffect — fire there.
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    });

    expect(onPick).toHaveBeenCalledTimes(1);
    const [picked] = onPick.mock.calls[0];
    expect(picked.key).toBe("transfer");
  });

  it("buildMessageFromSlash maps /transfer Nam 50k → 'chuyển cho Nam 50k'", () => {
    const cmd = SLASH_COMMANDS.find((c) => c.key === "transfer")!;
    expect(buildMessageFromSlash(cmd, "/transfer Nam 50k")).toBe(
      "chuyển cho Nam 50k",
    );
  });

  it("buildMessageFromSlash maps /history → 'lịch sử tháng này'", () => {
    const cmd = SLASH_COMMANDS.find((c) => c.key === "history")!;
    expect(buildMessageFromSlash(cmd, "/history")).toBe("lịch sử tháng này");
    expect(buildMessageFromSlash(cmd, "/history tháng trước")).toBe(
      "lịch sử tháng trước",
    );
  });

  it("buildMessageFromSlash returns send-text for /balance", () => {
    const cmd = SLASH_COMMANDS.find((c) => c.key === "balance")!;
    expect(buildMessageFromSlash(cmd, "/balance")).toBe("số dư");
  });
});
