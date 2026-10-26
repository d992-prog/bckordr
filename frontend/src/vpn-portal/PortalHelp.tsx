import { useState } from "react";

import { CLIENT_PLATFORMS, detectClientPlatform, type ClientPlatform } from "./clientApps";

const SCENARIOS = [
  ["Приложение не установлено", "Установите Happ или Hiddify по ссылке для своей платформы. Вернитесь на главную и откройте подписку в приложении", "#connect", "Перейти к подключению"],
  ["Импорт не открылся", "Откройте приложение вручную и добавьте подписку из буфера обмена. На главной доступны копирование ссылки, QR-код и ручной вариант", "#connect", "Скопировать ссылку или открыть QR-код"],
  ["Сайты не открываются", "Обновите подписку в VPN-приложении, выберите профиль и включите VPN. Если проблема осталась, выберите другую доступную страну и переподключитесь", "#devices", "Проверить устройство и страну"],
  ["Пропала скорость", "Попробуйте другую доступную страну, затем обновите подписку и переподключитесь в приложении. Кабинет не измеряет скорость вашего соединения", "#devices", "Выбрать страну"],
  ["Закончился доступ", "Проверьте срок подписки. Если вы ожидали продление, обратитесь в поддержку, когда контакт доступен в этом разделе", "#subscription", "Проверить доступ"],
  ["Потеряно устройство", "Откройте нужное устройство и подтвердите отключение. Доступ отключится во всех его странах; место освободится после подтверждения", "#devices", "Управлять устройствами"],
  ["Ссылка попала посторонним", "У нужного устройства выберите «Заменить доступ». Прежняя ссылка перестанет работать; после завершения подключите это устройство заново", "#devices", "Заменить доступ устройства"],
] as const;

export function PortalHelp({ supportUrl, supportText }: { supportUrl: string | null; supportText: string }) {
  const [platform, setPlatform] = useState<ClientPlatform>(() => detectClientPlatform(navigator.userAgent, navigator.maxTouchPoints));
  const client = CLIENT_PLATFORMS.find((item) => item.id === platform)!;
  return <section id="help" className="portal-help portal-section"><h1>Помощь</h1>
    <section className="vx-matte"><h2>Как подключиться</h2><label className="help-platform">Ваша платформа<select value={platform} onChange={(event) => setPlatform(event.target.value as ClientPlatform)}>{CLIENT_PLATFORMS.map((item) => <option key={item.id} value={item.id}>{item.id === "iphone" ? "iPhone и iPad" : item.label}</option>)}</select></label>
      <ol className="steps"><li>Установите {client.app}. <a href={client.download} target="_blank" rel="noreferrer">Скачать {client.app}</a></li><li><a href="#connect">Добавьте подписку с главной</a> через «Открыть в {client.app}», копирование или QR-код</li><li>Выберите импортированный профиль и включите VPN в приложении</li></ol><p className="hint">Открытие приложения не подтверждает импорт или VPN-подключение</p>
    </section>
    <section className="vx-matte"><h2>Если нужна помощь</h2>{SCENARIOS.map(([title, body, href, action]) => <details className="help-scenario" key={title}><summary>{title}</summary><p>{body}</p><a href={href}>{action}</a></details>)}</section>
    {(supportText || supportUrl) && <section className="vx-matte"><h2>Поддержка</h2>{supportText && <p>{supportText}</p>}{supportUrl && <a className="button button--primary" href={supportUrl} target="_blank" rel="noreferrer">Написать в поддержку</a>}</section>}
  </section>;
}
