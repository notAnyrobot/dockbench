declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: { credentials?: { password?: string } });
    scaleViewport: boolean;
    viewOnly: boolean;
    disconnect(): void;
    sendCtrlAltDel(): void;
  }
}
