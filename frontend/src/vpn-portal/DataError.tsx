interface DataErrorProps {
  message: string;
  onRetry: () => void;
}

export function DataError({ message, onRetry }: DataErrorProps) {
  return (
    <div className="card message message--error" role="alert">
      <p>{message}</p>
      <button className="button button--primary" onClick={onRetry}>Повторить</button>
    </div>
  );
}
