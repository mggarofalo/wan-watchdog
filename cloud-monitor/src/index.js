import { Monitor } from './monitor.js';

export class WanMonitor {
  constructor(context, env) {
    this.monitor = new Monitor(context.storage, env);
  }

  async fetch(request) {
    if (request.method !== 'POST' || new URL(request.url).pathname !== '/check') {
      return new Response('Not found', { status: 404 });
    }
    const { scheduledTime } = await request.json();
    return Response.json(await this.monitor.check(scheduledTime));
  }
}

export default {
  async scheduled(controller, env) {
    const instance = env.MONITOR.get(env.MONITOR.idFromName('home'));
    const response = await instance.fetch('https://monitor.internal/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scheduledTime: controller.scheduledTime }),
    });
    if (!response.ok) throw new Error(`Monitor failed: HTTP ${response.status}`);
    await response.body?.cancel();
  },

  fetch() {
    return new Response('Not found', { status: 404 });
  },
};
