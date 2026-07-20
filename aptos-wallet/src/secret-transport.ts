import type { EncryptedSecretResponse } from '../shared/types'

function toPem(bytes: ArrayBuffer): string {
  const binary = String.fromCharCode(...new Uint8Array(bytes))
  const base64 = btoa(binary)
  const lines = base64.match(/.{1,64}/g)?.join('\n') ?? base64
  return `-----BEGIN PUBLIC KEY-----\n${lines}\n-----END PUBLIC KEY-----`
}

function fromBase64(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0))
}

export async function requestEncryptedSecret(
  send: (publicKey: string) => Promise<EncryptedSecretResponse>,
): Promise<string> {
  const keyPair = await crypto.subtle.generateKey(
    { name: 'RSA-OAEP', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    false,
    ['encrypt', 'decrypt'],
  )
  const publicKey = toPem(await crypto.subtle.exportKey('spki', keyPair.publicKey))
  const response = await send(publicKey)
  if (response.algorithm !== 'RSA-OAEP-256') throw new Error('不支持的秘密传输算法')
  const plaintext = await crypto.subtle.decrypt({ name: 'RSA-OAEP' }, keyPair.privateKey, fromBase64(response.ciphertext))
  return new TextDecoder().decode(plaintext)
}
