/**
 * CAD Socket Client
 * Connects to the Blender/FreeCAD addon TCP servers using
 * length-prefixed JSON protocol with auto-reconnect and heartbeat.
 */

import net from 'net';

// Cap the receive buffer to defend against an unresponsive or malicious
// peer continuously sending bytes. Slightly larger than the addon's 50 MB
// per-message limit to leave room for one in-flight message + framing.
const MAX_RECEIVE_BUFFER = 64 * 1024 * 1024;

export class CADSocketClient {
  constructor(host, port, name) {
    this.host = host;
    this.port = port;
    this.name = name;
    this.socket = null;
    this.connected = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 3;
    this.pendingResolve = null;
    this.pendingReject = null;
    this.receiveBuffer = Buffer.alloc(0);
  }

  async connect() {
    return new Promise((resolve, reject) => {
      this.socket = new net.Socket();
      this.socket.setTimeout(10000);

      this.socket.connect(this.port, this.host, () => {
        this.connected = true;
        this.reconnectAttempts = 0;
        console.error(`CAD-MCP: Connected to ${this.name} at ${this.host}:${this.port}`);
        resolve();
      });

      this.socket.on('error', (err) => {
        if (!this.connected) {
          reject(new Error(
            `Cannot connect to ${this.name} at ${this.host}:${this.port}. ` +
            `Make sure ${this.name} is open and the CAD-MCP addon is running. Error: ${err.message}`
          ));
        } else {
          console.error(`CAD-MCP: ${this.name} socket error: ${err.message}`);
          this.connected = false;
          if (this.pendingReject) {
            this.pendingReject(err);
            this.pendingResolve = null;
            this.pendingReject = null;
          }
        }
      });

      this.socket.on('close', () => {
        this.connected = false;
        console.error(`CAD-MCP: ${this.name} connection closed`);
      });

      this.socket.on('data', (data) => {
        this._onData(data);
      });
    });
  }

  _onData(data) {
    if (this.receiveBuffer.length + data.length > MAX_RECEIVE_BUFFER) {
      const err = new Error(
        `Receive buffer overflow from ${this.name} ` +
        `(>${MAX_RECEIVE_BUFFER} bytes) — closing connection`
      );
      console.error(`CAD-MCP: ${err.message}`);
      this.socket.destroy();
      this.connected = false;
      this.receiveBuffer = Buffer.alloc(0);
      if (this.pendingReject) {
        const reject = this.pendingReject;
        this.pendingResolve = null;
        this.pendingReject = null;
        reject(err);
      }
      return;
    }
    this.receiveBuffer = Buffer.concat([this.receiveBuffer, data]);
    this._processBuffer();
  }

  _processBuffer() {
    while (this.receiveBuffer.length >= 4) {
      const msgLength = this.receiveBuffer.readUInt32BE(0);
      // Mirrors the addon's 50 MB per-message cap.
      if (msgLength > 50 * 1024 * 1024) {
        const err = new Error(
          `Refusing oversized message from ${this.name}: ${msgLength} bytes`
        );
        console.error(`CAD-MCP: ${err.message}`);
        this.socket.destroy();
        this.connected = false;
        this.receiveBuffer = Buffer.alloc(0);
        if (this.pendingReject) {
          const reject = this.pendingReject;
          this.pendingResolve = null;
          this.pendingReject = null;
          reject(err);
        }
        return;
      }
      if (this.receiveBuffer.length < 4 + msgLength) {
        break; // Wait for more data
      }
      const payload = this.receiveBuffer.slice(4, 4 + msgLength);
      this.receiveBuffer = this.receiveBuffer.slice(4 + msgLength);

      try {
        const message = JSON.parse(payload.toString('utf-8'));
        // Handle heartbeats silently
        if (message.type === 'heartbeat') {
          continue;
        }
        // Resolve pending request
        if (this.pendingResolve) {
          this.pendingResolve(message);
          this.pendingResolve = null;
          this.pendingReject = null;
        }
      } catch (err) {
        console.error(`CAD-MCP: Parse error from ${this.name}: ${err.message}`);
        if (this.pendingReject) {
          this.pendingReject(err);
          this.pendingResolve = null;
          this.pendingReject = null;
        }
      }
    }
  }

  async sendCommand(type, params = {}) {
    if (!this.connected) {
      // Try to reconnect
      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectAttempts++;
        console.error(`CAD-MCP: Reconnecting to ${this.name} (attempt ${this.reconnectAttempts})...`);
        try {
          await this.connect();
        } catch (err) {
          throw new Error(`Failed to reconnect to ${this.name}: ${err.message}`);
        }
      } else {
        throw new Error(
          `Not connected to ${this.name}. Make sure the application is open ` +
          `and the CAD-MCP addon server is running on port ${this.port}.`
        );
      }
    }

    const message = { type, params };
    const payload = Buffer.from(JSON.stringify(message), 'utf-8');
    const header = Buffer.alloc(4);
    header.writeUInt32BE(payload.length, 0);

    return new Promise((resolve, reject) => {
      this.pendingResolve = resolve;
      this.pendingReject = reject;

      const timeoutMs = 60000;
      const timer = setTimeout(() => {
        this.pendingResolve = null;
        this.pendingReject = null;
        reject(new Error(`Command '${type}' timed out after ${timeoutMs / 1000}s in ${this.name}`));
      }, timeoutMs);

      const originalResolve = this.pendingResolve;
      this.pendingResolve = (data) => {
        clearTimeout(timer);
        originalResolve(data);
      };
      const originalReject = this.pendingReject;
      this.pendingReject = (err) => {
        clearTimeout(timer);
        originalReject(err);
      };

      this.socket.write(Buffer.concat([header, payload]));
    });
  }

  async ping() {
    try {
      const result = await this.sendCommand('ping');
      return result?.result?.status === 'pong';
    } catch {
      return false;
    }
  }

  disconnect() {
    if (this.socket) {
      this.socket.destroy();
      this.socket = null;
    }
    this.connected = false;
  }
}
