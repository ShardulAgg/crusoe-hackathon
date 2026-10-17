import { useRef, useState, type KeyboardEvent } from 'react';
import Resources from './Resources';
import Benchmarks from './Benchmarks';
import Guide from './Guide';

const tabs = ['Benchmarks', 'Library', 'Agents', 'Resources', 'Guide'] as const;

export default function App() {
  const [activeTab, setActiveTab] = useState<(typeof tabs)[number]>('Benchmarks');
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex = index;

    switch (event.key) {
      case 'ArrowRight':
        nextIndex = (index + 1) % tabs.length;
        break;
      case 'ArrowLeft':
        nextIndex = (index - 1 + tabs.length) % tabs.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = tabs.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    setActiveTab(tabs[nextIndex]);
    tabRefs.current[nextIndex]?.focus();
  }

  return (
    <div className="dashboard">
      <div className="tabs" role="tablist" aria-label="Dashboard">
        {tabs.map((tab, index) => (
          <button
            key={tab}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            className="tab"
            id={`${tab.toLowerCase()}-tab`}
            type="button"
            role="tab"
            aria-selected={activeTab === tab}
            aria-controls={`${tab.toLowerCase()}-panel`}
            tabIndex={activeTab === tab ? 0 : -1}
            onClick={() => setActiveTab(tab)}
            onKeyDown={(event) => handleKeyDown(event, index)}
          >
            {tab}
          </button>
        ))}
      </div>

      <main className="content">
        {tabs.map((tab) => (
          <section
            key={tab}
            id={`${tab.toLowerCase()}-panel`}
            role="tabpanel"
            aria-labelledby={`${tab.toLowerCase()}-tab`}
            hidden={activeTab !== tab}
            tabIndex={0}
          >
            {tab === 'Benchmarks' && <Benchmarks active={activeTab === 'Benchmarks'} />}
            {tab === 'Resources' && <Resources active={activeTab === 'Resources'} />}
            {tab === 'Guide' && <Guide />}
          </section>
        ))}
      </main>
    </div>
  );
}
