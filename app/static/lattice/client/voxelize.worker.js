/**
 * 体素化 Web Worker：把 mesh → SDF 放到后台线程，别卡住渲染与交互。
 *
 * 主线程用 `new Worker(new URL('./client/voxelize.worker.js', import.meta.url),
 * { type: 'module' })` 起它，然后 postMessage({positions, indices, options})。
 *
 * 消息约定：
 *   收 {id, positions, indices, options}
 *   发 {id, type: 'progress', fraction, stage}
 *      {id, type: 'done', result}          // result.sdf 用 Transferable 零拷贝回传
 *      {id, type: 'error', message}
 */

import { voxelize } from './voxelize.js';

self.onmessage = (event) => {
  const { id, positions, indices, options } = event.data || {};
  if (!positions || !indices) {
    self.postMessage({ id, type: 'error', message: '缺少网格数据' });
    return;
  }

  try {
    const result = voxelize(
      { positions, indices },
      {
        ...(options || {}),
        onProgress: (fraction, stage) => {
          self.postMessage({ id, type: 'progress', fraction, stage });
        },
      },
    );
    // SDF 可能几十 MB，转移所有权避免复制
    self.postMessage({ id, type: 'done', result }, [result.sdf.buffer]);
  } catch (err) {
    self.postMessage({ id, type: 'error', message: String((err && err.message) || err) });
  }
};
