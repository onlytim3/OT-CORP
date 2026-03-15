import { useState, useRef, useEffect } from "react";
import { MessageSquare, Send, X, AlertCircle, CheckCircle2 } from "lucide-react";
import { api, fetchAPI, type ChatMessage } from "../config/api";

export function ChatPanel({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: 'assistant', content: 'Hello! I can help you manage your trading system. Ask me about positions, strategies, or give me commands.' },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async () => {
    if (!input.trim() || loading) return;

    const userMsg = input.trim();
    setInput('');
    setMessages(prev => [...prev, { role: 'user', content: userMsg }]);
    setLoading(true);

    try {
      const res = await fetchAPI<{ answer?: string; action_id?: string; action?: string; confirmation_required?: boolean; message?: string }>(
        api.chat,
        { method: 'POST', body: JSON.stringify({ message: userMsg }) }
      );

      if (res.confirmation_required && res.action_id) {
        setPendingAction(res.action_id);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: res.message || `Confirm action: ${res.action}`,
          action_id: res.action_id,
          confirmation_required: true,
        }]);
      } else {
        setMessages(prev => [...prev, { role: 'assistant', content: res.answer || res.message || 'Done.' }]);
      }
    } catch {
      setMessages(prev => [...prev, { role: 'assistant', content: 'Failed to reach the backend. Is the dashboard server running?' }]);
    } finally {
      setLoading(false);
    }
  };

  const confirmAction = async (actionId: string) => {
    setLoading(true);
    try {
      const res = await fetchAPI<{ answer?: string; status?: string }>(
        api.chatConfirm,
        { method: 'POST', body: JSON.stringify({ action_id: actionId }) }
      );
      setMessages(prev => [...prev, { role: 'assistant', content: res.answer || `Action ${res.status || 'completed'}.` }]);
      setPendingAction(null);
    } catch {
      setMessages(prev => [...prev, { role: 'assistant', content: 'Failed to confirm action.' }]);
    } finally {
      setLoading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed bottom-24 right-4 w-96 max-h-[70vh] flex flex-col bg-black border border-white/8 rounded-lg z-50 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/8 bg-[#0a0a0a]">
        <div className="flex items-center gap-2">
          <MessageSquare className="size-5 text-[#4a9eff]" />
          <span className="font-semibold text-[#e8e8e8]">Trading Assistant</span>
        </div>
        <button onClick={onClose} className="p-1 hover:bg-white/10 rounded-lg transition-colors">
          <X className="size-5 text-[#888888]" />
        </button>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3 min-h-[200px] max-h-[50vh]">
        {messages.map((msg, idx) => (
          <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm ${
              msg.role === 'user'
                ? 'bg-[#4a9eff] text-white rounded-br-md'
                : 'bg-white/5 text-[#e8e8e8] border border-white/10 rounded-bl-md'
            }`}>
              <p className="whitespace-pre-wrap">{msg.content}</p>
              {msg.confirmation_required && msg.action_id && (
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={() => confirmAction(msg.action_id!)}
                    disabled={loading || pendingAction !== msg.action_id}
                    className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[#00d4aa]/20 text-[#00d4aa] border border-[#00d4aa]/30 text-xs hover:bg-[#00d4aa]/30 transition-colors disabled:opacity-50"
                  >
                    <CheckCircle2 className="size-3" /> Confirm
                  </button>
                  <button
                    onClick={() => setPendingAction(null)}
                    className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[#ff4466]/20 text-[#ff4466] border border-[#ff4466]/30 text-xs hover:bg-[#ff4466]/30 transition-colors"
                  >
                    <AlertCircle className="size-3" /> Cancel
                  </button>
                </div>
              )}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-white/5 border border-white/10 rounded-2xl rounded-bl-md px-4 py-3">
              <div className="flex gap-1">
                <div className="size-2 rounded-full bg-[#888888] animate-bounce" style={{ animationDelay: '0ms' }} />
                <div className="size-2 rounded-full bg-[#888888] animate-bounce" style={{ animationDelay: '150ms' }} />
                <div className="size-2 rounded-full bg-[#888888] animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="px-4 py-3 border-t border-white/8 bg-black">
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && sendMessage()}
            placeholder="Ask about positions, strategies..."
            className="flex-1 bg-white/5 border border-white/10 rounded-xl px-4 py-2.5 text-sm text-[#e8e8e8] placeholder:text-[#888888] focus:outline-none focus:border-[#4a9eff]/50"
            disabled={loading}
          />
          <button
            onClick={sendMessage}
            disabled={!input.trim() || loading}
            className="p-2.5 rounded-xl bg-[#4a9eff] text-white hover:bg-[#4a9eff]/80 transition-colors disabled:opacity-50"
          >
            <Send className="size-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
